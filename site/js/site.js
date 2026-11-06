/* wouf.dev — what v8 leaves to the site: the bar's top and bottom states (v8's scroll module sets
   html[data-scroll]), the demo, the section lock, the current docs section, and copying with a spoken result.
   State lives in data-attributes and CSS custom properties set through the CSSOM; no inline styles. */

/* index.html leaves v8's copy module out (html[data-vaiev-manual]) so that each copy attempt reports its own
   result below; every other v8 module starts as usual. */
(() => {
  const root = document.documentElement;
  const ui = globalThis.VaievUI;
  if (!ui || !root.hasAttribute || !root.hasAttribute('data-vaiev-manual')) return;
  Object.entries(ui).forEach(([name, start]) => { if (name !== 'copy') start(); });
})();

/* the site bar: top (part of the canvas), down (only Docs, from 860px), up, bottom (everything, whatever the
   direction). Phones keep Home, Examples and Docs at all times. While focus is anywhere in the bar it never
   folds: every link stays where a keyboard expects it. */
(() => {
  const root = document.documentElement;
  const bar = document.querySelector('.site-bar');
  if (!bar) return;
  const links = [...bar.querySelector('.site-links').querySelectorAll('a')];
  const mobile = matchMedia('(max-width: 859px)');
  const TOP = 64, BOTTOM = 96;
  let queued = false;
  function update() {
    queued = false;
    const focused = bar.contains(document.activeElement);
    const top = scrollY < TOP;
    const bottom = !top && scrollY + innerHeight >= root.scrollHeight - BOTTOM;
    const down = root.dataset.scroll === 'down' && !focused;
    const state = top ? 'top' : bottom ? 'bottom' : down ? 'down' : 'up';
    root.dataset.siteState = state;
    // folded (wide screens only), only Docs stays
    const fold = state === 'down' && !mobile.matches;
    links.forEach(link => { link.hidden = fold && link.textContent.trim() !== 'Docs'; });
  }
  function schedule() { if (!queued) { queued = true; requestAnimationFrame(() => requestAnimationFrame(update)); } }
  // focus coming in unfolds the bar at once, before the next Tab; focus leaving lets it fold again
  bar.addEventListener('focusin', update);
  bar.addEventListener('focusout', () => requestAnimationFrame(schedule));
  bar.addEventListener('pointerdown', schedule);
  addEventListener('scroll', schedule, { passive: true });
  addEventListener('resize', schedule);
  addEventListener('hashchange', schedule);
  addEventListener('load', schedule);
  if (mobile.addEventListener) mobile.addEventListener('change', schedule);
  schedule();
})();

/* the demo, for any [data-demo]: it plays its dialogs line by line, typing what you say into the composer
   first, then moves on to the next one. Chips choose a dialog, Pause/Play and Restart control it; it rests
   while out of view or in a hidden tab. With reduced motion every dialog shows finished, without movement. */
(() => {
  const reduce = matchMedia('(prefers-reduced-motion: reduce)');
  const TYPE = 38, AFTER = { you: 700, lead: 1500, done: 1300 }, REST = 5200;
  document.querySelectorAll('[data-demo]').forEach(demo => {
    const chips = [...demo.querySelectorAll('button[data-scenario]')];
    const dialogs = [...demo.querySelectorAll('.demo-dialog')];
    const toggle = demo.querySelector('[data-demo-toggle]');
    const restart = demo.querySelector('[data-demo-restart]');
    const typed = demo.querySelector('[data-demo-typed]');
    if (!dialogs.length) return;
    let current = Math.max(0, dialogs.findIndex(d => d.hasAttribute('data-active')));
    let step = 0, chars = 0, timer = 0, paused = false, away = false;
    const lines = d => [...d.querySelectorAll('.msg')];
    const say = text => { if (typed) typed.textContent = text; };
    function stop() { clearTimeout(timer); timer = 0; }
    function later(ms) { stop(); if (!paused && !away && !reduce.matches) timer = setTimeout(tick, ms); }
    function choose(index) {
      current = (index + dialogs.length) % dialogs.length;
      dialogs.forEach((d, i) => d.toggleAttribute('data-active', i === current));
      chips.forEach(c => c.setAttribute('aria-pressed', String(c.dataset.scenario === dialogs[current].dataset.scenario)));
      step = 0; chars = 0; say('');
      dialogs.forEach(d => lines(d).forEach(l => l.toggleAttribute('data-shown', reduce.matches)));
    }
    function tick() {
      const all = lines(dialogs[current]);
      if (step >= all.length) { choose(current + 1); later(600); return; }
      const line = all[step];
      const kind = line.classList.contains('msg-you') ? 'you' : line.classList.contains('msg-done') ? 'done' : 'lead';
      const text = (line.querySelector('p') || line).textContent;
      if (kind === 'you' && chars < text.length) { chars += 1; say(text.slice(0, chars)); later(TYPE); return; }
      say(''); chars = 0;
      line.setAttribute('data-shown', '');
      step += 1;
      later(step >= all.length ? REST : AFTER[kind]);
    }
    function setPaused(on) {
      paused = on;
      if (toggle) toggle.textContent = on ? 'Play' : 'Pause';
      if (on) stop(); else later(300);
    }
    function still() {
      demo.toggleAttribute('data-still', reduce.matches);
      stop(); choose(current);
      if (!reduce.matches) later(500);
    }
    chips.forEach((chip, i) => chip.addEventListener('click', () => {
      choose(dialogs.findIndex(d => d.dataset.scenario === chip.dataset.scenario));
      later(400);
    }));
    if (toggle) toggle.addEventListener('click', () => setPaused(!paused));
    if (restart) restart.addEventListener('click', () => { choose(current); setPaused(false); });
    if ('IntersectionObserver' in globalThis) {
      new IntersectionObserver(entries => {
        away = !entries[entries.length - 1].isIntersecting;
        if (away) stop(); else later(400);
      }).observe(demo);
    }
    document.addEventListener('visibilitychange', () => { away = document.hidden; if (away) stop(); else later(400); });
    if (reduce.addEventListener) reduce.addEventListener('change', still);
    demo.setAttribute('data-ready', '');
    still();
  });
})();

/* the section lock: where scroll timelines exist and motion is welcome, .deck[data-lock] lets each band stick
   once it is fully in view (css/site.css). --lock-top is 100px under the bar, or, for a band taller than the
   window, the place where its end meets the bottom (above the phone bar), so every line can be read before
   the next band covers it. A keyboard focus in the deck releases the lock and brings the focus into view. */
(() => {
  const deck = document.querySelector('.deck');
  if (!deck) return;
  const bands = [...deck.children].filter(el => el.classList.contains('band'));
  const can = !!(globalThis.CSS && CSS.supports && CSS.supports('animation-timeline: view()') && CSS.supports('timeline-scope: --band-2'));
  const reduce = matchMedia('(prefers-reduced-motion: reduce)');
  const mobile = matchMedia('(max-width: 859px)');
  const GAP = 16, UNDER_BAR = 100;
  let queued = false;
  function measure() {
    queued = false;
    const on = can && !reduce.matches;
    deck.toggleAttribute('data-lock', on);
    if (!on) return;
    const bar = document.querySelector('.site-bar');
    const top = mobile.matches ? GAP : UNDER_BAR;
    const floor = mobile.matches && bar ? bar.getBoundingClientRect().top - GAP : innerHeight - GAP;
    bands.forEach(band => {
      band.style.setProperty('--lock-top', Math.round(Math.min(top, floor - band.offsetHeight)) + 'px');
    });
  }
  function schedule() { if (!queued) { queued = true; requestAnimationFrame(measure); } }
  if ('ResizeObserver' in globalThis) { const watch = new ResizeObserver(schedule); bands.forEach(b => watch.observe(b)); }
  addEventListener('resize', schedule);
  if (reduce.addEventListener) reduce.addEventListener('change', schedule);
  deck.addEventListener('focusin', e => {
    if (deck.hasAttribute('data-lock')) requestAnimationFrame(() => e.target.scrollIntoView({ block: 'nearest' }));
  });
  measure();
})();

/* docs: the last section heading above the line under the bar is the current one (aria-current="location");
   the first before any heading, the last at the very bottom. The chip row on phones keeps it in view. */
(() => {
  const docsNav = document.querySelector('.docs-nav');
  if (!docsNav) return;
  const LINE = 104;
  const items = [...docsNav.querySelectorAll('a')]
    .map(a => ({ a, head: document.getElementById((a.getAttribute('href') || '').slice(1)) }))
    .filter(item => item.head);
  if (!items.length) return;
  const reduce = matchMedia('(prefers-reduced-motion: reduce)');
  let current = null, queued = false;
  function more() {
    docsNav.toggleAttribute('data-more', docsNav.scrollLeft + docsNav.clientWidth < docsNav.scrollWidth - 2);
  }
  function mark() {
    queued = false;
    let at = items[0];
    for (const item of items) if (item.head.getBoundingClientRect().top <= LINE + 1) at = item;
    if (scrollY + innerHeight >= document.documentElement.scrollHeight - 2) at = items[items.length - 1];
    if (at !== current) {
      if (current) current.a.removeAttribute('aria-current');
      at.a.setAttribute('aria-current', 'location');
      current = at;
      // a horizontal scroll of the row only: never the page, never focus
      if (docsNav.scrollWidth > docsNav.clientWidth + 2) {
        const left = at.a.offsetLeft - (docsNav.clientWidth - at.a.offsetWidth) / 2;
        docsNav.scrollTo({ left: Math.max(0, left), behavior: reduce.matches ? 'auto' : 'smooth' });
      }
    }
    more();
  }
  function schedule() { if (!queued) { queued = true; requestAnimationFrame(mark); } }
  docsNav.addEventListener('scroll', more, { passive: true });
  addEventListener('scroll', schedule, { passive: true });
  addEventListener('resize', schedule);
  addEventListener('load', schedule);
  mark();
})();

/* copy: say what happened. Each press copies the sentence itself and reports that attempt's own result; a
   newer press wins over an older one still waiting. When the clipboard refuses, the sentence is selected, so
   it can be copied by hand. */
(() => {
  const status = document.getElementById('copy-status');
  const buttons = [...document.querySelectorAll('[data-copy]')];
  if (!status || !buttons.length) return;
  let attempt = 0;
  const say = text => { status.textContent = ''; setTimeout(() => { status.textContent = text; }, 50); };
  function select(source) {
    const range = document.createRange();
    range.selectNodeContents(source);
    const selection = getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
  }
  buttons.forEach(button => button.addEventListener('click', () => {
    const source = document.querySelector(button.dataset.copy);
    if (!source) return;
    const mine = ++attempt;
    buttons.forEach(b => b.removeAttribute('data-done'));
    const done = ok => {
      if (mine !== attempt) return;
      if (ok) {
        button.setAttribute('data-done', '');
        setTimeout(() => { if (mine === attempt) button.removeAttribute('data-done'); }, 1600);
        say('Copied. Paste it into your AI assistant.');
      } else {
        select(source);
        say('Copying is not allowed here. The sentence is selected: copy it with your keyboard or menu.');
      }
    };
    let write;
    try { write = navigator.clipboard.writeText(source.textContent.trim()); } catch (_) { write = Promise.reject(); }
    Promise.resolve(write).then(() => done(true), () => done(false));
  }));
})();
