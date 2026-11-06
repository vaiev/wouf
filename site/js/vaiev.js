/* VAIEV UI v8 — behaviours. Small, dependency-free modules; each does nothing when its markup is missing.
   No inline styles in markup: modules only toggle attributes and write CSS custom properties through CSSOM,
   so pages run under Content-Security-Policy default-src 'self'. Load with <script src="…/vaiev.js" defer>.

   scroll      html[data-scroll="up|down"] — the Morph Bar and masthead fold while scrolling down
   carousels   pill dots that follow the snapped item
   peek        hold a [data-peek] medium to see it full screen; a video plays only while held
   omni        the OmniSearch dialog: commands, filtering, "/" to open, saved posts below
   invokers    command="show-modal" / "close" for browsers without invoker commands
   votes       up/down votes toggle and count
   bars        --pct from data-pct for poll bars and rings
   segFilter   a segmented control with data-filter filters the feed by data-kind
   approve     the Trusted Bar asks before an action; on "Allow" the card folds into a confirmation
   clock       analog clock tiles and [data-time] text follow the real time
   share       [data-share] uses the system share sheet, or copies the link
   states      a segmented control with data-states-for sets data-state on its target (Morph Bar demo)
   copy        [data-copy="#id"] copies that element's text
   mail        [data-mail] shows an address only on request; it is not in the page as text
   hours       opening hours: marks today and says whether it is open now, in the table's time zone
   boardMenu   the board's own bar item: its tip menu sorts and filters at the top, it scrolls up further down
   replyTo     answering a reply puts a context chip above the Omniboard
   toggles     [data-toggle] buttons flip aria-pressed
   objects     things put in front of you for a while show how long they stay, then leave
   ask         the one prompt: send, let the field work, show the surface made for the question
   inbox       decisions that fold into a confirmation and leave; an empty state when none are left
   tabs        ARIA tabs with arrow keys
   kanban      cards move between columns by drag or Alt+Arrow; limits and what a move starts
   chatPanel   the side panel to talk to the lead, opened with a question from the context
   drafts      a plan's draft cards: reorder, strike out, approve
   Page transitions live in js/vt.js, which loads in <head> without defer. */
(() => {
  const root = document.documentElement;
  const reduce = matchMedia('(prefers-reduced-motion: reduce)');
  const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];
  // a state change as a view transition; when a newer one starts first, the older one is skipped quietly
  const transition = update => {
    if (!document.startViewTransition || reduce.matches) return update();
    const vt = document.startViewTransition(update);
    vt.ready.catch(() => {});
    vt.finished.catch(() => {});
    return vt;
  };

  // ── scroll direction: folds bars while reading downwards, shows them again on the way back ──
  function scroll() {
    let last = scrollY, ticking = false;
    root.dataset.scroll = 'up';
    addEventListener('scroll', () => {
      if (ticking) return;
      ticking = true;
      requestAnimationFrame(() => {
        const y = scrollY, d = y - last;
        if (y < 80) root.dataset.scroll = 'up';
        else if (Math.abs(d) > 6) root.dataset.scroll = d > 0 ? 'down' : 'up';
        last = y; ticking = false;
      });
    }, { passive: true });
    // a tap on the folded bar or masthead opens it again
    $$('[data-collapsible]').forEach(el => el.addEventListener('pointerdown', () => { root.dataset.scroll = 'up'; }));
  }

  // ── carousel dots ──
  function carousels() {
    $$('.carousel').forEach(c => {
      const track = c.querySelector('.carousel-track'), dots = c.querySelector('.carousel-dots');
      if (!track || !dots) return;
      const items = $$(':scope > .carousel-item', track);
      dots.replaceChildren(...items.map(() => document.createElement('span')));
      const mark = () => {
        const x = track.scrollLeft + parseFloat(getComputedStyle(track).scrollPaddingInlineStart || 0);
        let best = 0, dist = Infinity;
        items.forEach((it, i) => { const d = Math.abs(it.offsetLeft - x); if (d < dist) { dist = d; best = i; } });
        [...dots.children].forEach((d, i) => d.toggleAttribute('data-current', i === best));
      };
      track.addEventListener('scroll', () => requestAnimationFrame(mark), { passive: true });
      mark();
    });
  }

  // ── peek: hold to see full screen. Click or keyboard opens it until closed. ──
  function peek() {
    const media = $$('[data-peek]');
    if (!media.length) return;
    const dlg = document.createElement('dialog');
    dlg.className = 'peek';
    dlg.setAttribute('aria-label', document.documentElement.lang.startsWith('de') ? 'Bild ganz aaluege' : 'Full view');
    const stage = document.createElement('div');
    stage.className = 'peek-stage';
    const close = document.createElement('button');
    close.className = 'icon-btn lens peek-close';
    close.type = 'button';
    close.setAttribute('aria-label', root.lang.startsWith('de') ? 'Zuemache' : 'Close');
    close.innerHTML = '<svg class="icon" aria-hidden="true"><use href="' + iconHref('close') + '"/></svg>';
    const hint = document.createElement('p');
    hint.className = 'peek-hint lens';
    hint.textContent = root.lang.startsWith('de') ? 'Loslah zum Zuemache' : 'Release to close';
    dlg.append(stage, close, hint);
    document.body.append(dlg);
    let source = null;

    const open = (el, mode) => {
      const src = el.querySelector('img, video');
      if (!src) return;
      source = el;
      const copy = src.cloneNode(true);
      copy.removeAttribute('loading');
      if (copy.tagName === 'VIDEO') { copy.muted = true; copy.loop = true; copy.playsInline = true; copy.controls = false; }
      dlg.dataset.mode = mode;
      const show = () => { stage.replaceChildren(copy); dlg.showModal(); src.style.viewTransitionName = ''; if (copy.tagName === 'VIDEO') copy.play().catch(() => {}); };
      src.style.viewTransitionName = 'peek';
      transition(show);
      if (mode !== 'hold') close.focus();
    };
    const shut = () => {
      if (!dlg.open) return;
      const v = stage.querySelector('video');
      if (v) v.pause();
      const src = source && source.querySelector('img, video');
      transition(() => { dlg.close(); stage.replaceChildren(); if (src) src.style.viewTransitionName = 'peek'; });
      if (src) setTimeout(() => { src.style.viewTransitionName = ''; }, 600);
    };
    close.addEventListener('click', shut);
    dlg.addEventListener('close', () => { const v = stage.querySelector('video'); if (v) v.pause(); });
    dlg.addEventListener('click', e => { if (dlg.dataset.mode !== 'hold' && e.target === dlg) shut(); });

    media.forEach(el => {
      let timer = 0, held = false;
      const button = el.querySelector('.media-open');
      el.addEventListener('pointerdown', e => {
        if (e.button !== 0) return;
        held = false;
        el.dataset.pressing = '';
        el.setPointerCapture(e.pointerId);
        timer = setTimeout(() => { held = true; delete el.dataset.pressing; open(el, 'hold'); }, 320);
      });
      const end = () => {
        clearTimeout(timer);
        delete el.dataset.pressing;
        if (held) { shut(); held = false; }
      };
      el.addEventListener('pointerup', end);
      el.addEventListener('pointercancel', end);
      el.addEventListener('contextmenu', e => e.preventDefault());
      if (button) button.addEventListener('click', e => { if (held) { e.preventDefault(); return; } open(el, 'sticky'); });
    });
  }

  // ── OmniSearch ──
  function omni() {
    const dlg = document.querySelector('dialog.omni');
    if (!dlg) return;
    const input = dlg.querySelector('input[type="search"]');
    const cmds = $$('.omni-cmd', dlg);
    const saved = dlg.querySelector('.omni-saved');
    // the page's own bar comes along, with the hub slot as the current one
    const bar = document.querySelector('.dock .bar');
    if (bar && !dlg.querySelector('.dock')) {
      const dock = document.createElement('div');
      dock.className = 'dock';
      const copy = bar.cloneNode(true);
      copy.removeAttribute('data-collapsible');
      copy.style.viewTransitionName = 'none';
      $$('.bar-item', copy).forEach(it => {
        it.removeAttribute('aria-current');
        if (it.matches('[data-omni-open]')) { it.setAttribute('aria-current', 'page'); it.removeAttribute('data-omni-open'); it.addEventListener('click', () => dlg.close()); }
        // a bar item that opens a tip menu only closes the search here
        if (it.matches('[popovertarget]')) { it.removeAttribute('popovertarget'); it.removeAttribute('data-board-menu'); it.addEventListener('click', () => dlg.close()); }
      });
      dock.append(copy);
      dlg.append(dock);
    }
    const open = () => {
      if (dlg.open) return;
      dlg.showModal();
      dlg.scrollTop = 0;
      input && input.focus();
    };
    $$('[data-omni-open]').forEach(b => b.addEventListener('click', open));
    addEventListener('keydown', e => {
      const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName) || document.activeElement?.isContentEditable;
      if (e.key === '/' && !typing && !e.metaKey && !e.ctrlKey) { e.preventDefault(); open(); }
    });
    dlg.addEventListener('click', e => { if (e.target === dlg) dlg.close(); });
    if (!input) return;
    const filter = () => {
      const q = input.value.trim().toLowerCase();
      let first = null;
      cmds.forEach(c => {
        const hit = !q || c.textContent.toLowerCase().includes(q.replace(/^\//, ''));
        c.closest('li').hidden = !hit;
        c.toggleAttribute('data-active', false);
        if (hit && !first) first = c;
      });
      if (first && q) first.toggleAttribute('data-active', true);
      if (saved) $$('.post', saved).forEach(p => { p.hidden = !!q && !q.startsWith('/') && !p.textContent.toLowerCase().includes(q); });
    };
    input.addEventListener('input', filter);
    // Escape closes at once (a search field would otherwise only clear itself first); closing resets the field
    dlg.addEventListener('close', () => { input.value = ''; filter(); });
    input.addEventListener('keydown', e => {
      if (e.key === 'Escape') { e.preventDefault(); dlg.close(); return; }
      if (e.key !== 'Enter') return;
      const active = dlg.querySelector('.omni-cmd[data-active]');
      if (active) { e.preventDefault(); active.click(); }
    });
    // "↓ Gspycheret": the handle (and the saved-posts command) brings the saved posts into view
    const toSaved = () => saved && saved.scrollIntoView({ behavior: reduce.matches ? 'auto' : 'smooth', block: 'start' });
    const handle = dlg.querySelector('.omni-handle');
    if (handle) handle.addEventListener('click', toSaved);
    $$('[data-cmd]', dlg).forEach(b => b.addEventListener('click', () => {
      if (saved && b.dataset.cmd === '/gemerkt') { toSaved(); return; }
      input.value = `${b.dataset.cmd} `;
      input.focus();
      filter();
    }));
  }

  // ── invoker commands fallback ──
  function invokers() {
    if ('command' in HTMLButtonElement.prototype) return;
    $$('button[commandfor]').forEach(b => b.addEventListener('click', () => {
      const t = document.getElementById(b.getAttribute('commandfor'));
      const cmd = b.getAttribute('command');
      if (!t) return;
      if (cmd === 'show-modal' && !t.open) t.showModal();
      else if (cmd === 'close' && t.open) t.close();
    }));
  }

  // ── votes ──
  function votes() {
    $$('.vote').forEach(v => {
      const out = v.querySelector('output');
      const [up, down] = $$('button', v);
      if (!out || !up || !down) return;
      const base = parseInt(out.textContent, 10) || 0;
      const set = (btn, other) => {
        const on = btn.getAttribute('aria-pressed') !== 'true';
        btn.setAttribute('aria-pressed', String(on));
        other.setAttribute('aria-pressed', 'false');
        out.textContent = String(base + (up.getAttribute('aria-pressed') === 'true' ? 1 : 0) - (down.getAttribute('aria-pressed') === 'true' ? 1 : 0));
      };
      up.addEventListener('click', () => set(up, down));
      down.addEventListener('click', () => set(down, up));
    });
  }

  // ── bars and rings from data-pct / data-p ──
  function bars() {
    $$('[data-pct]').forEach(el => el.style.setProperty('--pct', `${parseFloat(el.dataset.pct)}%`));
    $$('[data-p]').forEach(el => el.style.setProperty('--p', parseFloat(el.dataset.p)));
  }

  // ── segmented filter → feed ──
  function segFilter() {
    $$('.seg[data-filter]').forEach(seg => {
      const feed = document.querySelector(seg.dataset.filter);
      if (!feed) return;
      seg.addEventListener('change', e => {
        const kind = e.target.value;
        transition(() => { $$(':scope > [data-kind]', feed).forEach(el => { el.hidden = kind !== 'aui' && !el.dataset.kind.split(' ').includes(kind); }); });
        const now = document.querySelector('.masthead-now');
        if (now) now.textContent = e.target.closest('label').textContent.trim();
      });
    });
  }

  // ── approve: the Trusted Bar asks first ──
  function approve() {
    $$('[data-approve]').forEach(trigger => trigger.addEventListener('click', () => {
      const bar = document.getElementById(trigger.dataset.approve);
      if (!bar) return;
      bar.hidden = false;
      // Allow is .btn-allow; the other answer is [data-deny] (or the remaining button)
      const allow = bar.querySelector('.btn-allow'), deny = bar.querySelector('[data-deny]') || bar.querySelector('.trusted-actions .btn:not(.btn-allow)');
      const done = ok => {
        bar.hidden = true;
        allow.removeEventListener('click', onAllow);
        deny.removeEventListener('click', onDeny);
        if (!ok) { trigger.focus(); return; }
        const target = document.querySelector(trigger.dataset.approvedTarget);
        if (target) transition(() => {
          const chip = document.createElement('p');
          chip.className = 'chip confirm';
          chip.setAttribute('role', 'status');
          chip.innerHTML = '<svg class="icon" aria-hidden="true"><use href="' + iconHref('check') + '"/></svg>';
          chip.append(document.createTextNode(trigger.dataset.approvedText || 'Done'));
          target.replaceChildren(chip);
          target.dispatchEvent(new CustomEvent('vv:approved', { bubbles: true }));
        });
      };
      const onAllow = () => done(true), onDeny = () => done(false);
      allow.addEventListener('click', onAllow);
      deny.addEventListener('click', onDeny);
      allow.focus();
    }));
  }

  // ── clock ──
  function clock() {
    const hands = $$('.clock'), times = $$('[data-time]');
    if (!hands.length && !times.length) return;
    const tick = () => {
      const d = new Date(), h = d.getHours(), m = d.getMinutes(), s = d.getSeconds();
      hands.forEach(c => {
        const set = (sel, deg) => { const el = c.querySelector(sel); if (el) el.style.transform = `rotate(${deg}deg)`; };
        set('.hand-h', (h % 12) * 30 + m / 2);
        set('.hand-m', m * 6);
        set('.hand-s', s * 6);
      });
      // data-time="" shows the viewer's time; data-time="Europe/Zurich" the time there, with its zone
      times.forEach(t => {
        const zone = t.dataset.time;
        t.textContent = zone
          ? new Intl.DateTimeFormat('en-GB', { timeZone: zone, hour: '2-digit', minute: '2-digit', timeZoneName: 'short' }).format(d)
          : `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}`;
      });
    };
    tick();
    setInterval(tick, 1000);
  }

  // ── share ──
  function share() {
    $$('[data-share]').forEach(b => b.addEventListener('click', async () => {
      const data = { title: document.title, url: location.href };
      try {
        if (navigator.share) await navigator.share(data);
        else { await navigator.clipboard.writeText(data.url); b.dataset.done = ''; setTimeout(() => delete b.dataset.done, 1600); }
      } catch (_) { /* dismissed */ }
    }));
  }

  // ── states: one control drives the state of a component (the Morph Bar demo) ──
  function states() {
    $$('[data-states-for]').forEach(seg => {
      const target = document.querySelector(seg.dataset.statesFor);
      if (!target) return;
      seg.addEventListener('change', e => transition(() => { target.dataset.state = e.target.value; }));
    });
  }

  // ── copy: [data-copy="#id"] copies that element's text and confirms with data-done ──
  function copy() {
    $$('[data-copy]').forEach(b => b.addEventListener('click', async () => {
      const src = document.querySelector(b.dataset.copy);
      if (!src) return;
      try { await navigator.clipboard.writeText(src.textContent.trim()); b.dataset.done = ''; setTimeout(() => delete b.dataset.done, 1600); }
      catch (_) { /* no clipboard: the text stays selectable */ }
    }));
  }

  // ── mail: the address is not in the page as text. A [data-mail] button holds it reversed in
  //    data-mail-u / data-mail-d; JS shows the button (the "x at y dot z" fallback stays without JS) and
  //    turns it into a mailto link on the first press. ──
  function mail() {
    $$('[data-mail]').forEach(b => {
      const flip = s => [...(s || '')].reverse().join('');
      const addr = `${flip(b.dataset.mailU)}@${flip(b.dataset.mailD)}`;
      const fallback = b.parentElement.querySelector('[data-mail-fallback]');
      b.hidden = false; if (fallback) fallback.hidden = true;
      b.addEventListener('click', () => {
        const a = document.createElement('a');
        a.className = b.className; a.href = `mailto:${addr}`; a.innerHTML = b.innerHTML;
        a.querySelector('.row-value').textContent = addr;
        b.replaceWith(a); a.focus();
      }, { once: true });
    });
  }

  // ── hours: a table[data-hours="<time zone>"] with rows tr[data-day="0-6" data-open="08:00-12:00,13:30-18:30"]
  //    marks today's row; [data-open-for="#table"] says whether it is open now (data-state open | closed) ──
  function hours() {
    $$('table[data-hours]').forEach(table => {
      const parts = Object.fromEntries(new Intl.DateTimeFormat('en-GB', { timeZone: table.dataset.hours || undefined, weekday: 'short', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' })
        .formatToParts(new Date()).map(p => [p.type, p.value]));
      const day = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'].indexOf(parts.weekday);
      const now = `${parts.hour}:${parts.minute}`;
      const row = table.querySelector(`tr[data-day="${day}"]`);
      if (row) row.dataset.today = '';
      const span = row && (row.dataset.open || '').split(',').map(r => r.trim().split('-')).find(([a, b]) => a && now >= a && now < b);
      $$(`[data-open-for="#${table.id}"]`).forEach(s => {
        s.dataset.state = span ? 'open' : 'closed';
        s.textContent = span ? `${s.dataset.openText} · ${s.dataset.untilText} ${span[1]}` : s.dataset.closedText;
        s.hidden = false;
      });
    });
  }

  // ── boardMenu: the current bar item of a board. At the top it opens its tip menu (popovertarget); further
  //    down it brings you back to the top. The menu sorts (name="sort": neu | beliebt) and filters
  //    (name="show": a data-kind, "aui" for all) the board named in its data-board; [data-board-now] says what shows. ──
  function boardMenu() {
    $$('[data-board-menu]').forEach(btn => {
      const menu = document.querySelector(btn.dataset.boardMenu);
      const feed = menu && document.querySelector(menu.dataset.board);
      if (!feed) return;
      btn.addEventListener('click', e => {
        if (scrollY < 80) return;
        e.preventDefault();
        scrollTo({ top: 0, behavior: reduce.matches ? 'auto' : 'smooth' });
      });
      const original = [...feed.children];
      const score = el => +((el.querySelector('.vote output') || {}).textContent || 0);
      const now = document.querySelector('[data-board-now]');
      const say = () => { if (now) now.textContent = $$('input:checked', menu).map(i => i.dataset.label).join(' · '); };
      menu.addEventListener('change', e => {
        const input = e.target;
        transition(() => {
          if (input.name === 'sort') {
            // posts change places; carousels keep theirs
            const posts = original.filter(el => el.matches('article.post'));
            const order = input.value === 'beliebt' ? [...posts].sort((a, b) => score(b) - score(a)) : posts;
            let k = 0;
            feed.append(...original.map(el => (el.matches('article.post') ? order[k++] : el)));
          }
          if (input.name === 'show') {
            $$(':scope > [data-kind]', feed).forEach(el => { el.hidden = input.value !== 'aui' && !el.dataset.kind.split(' ').includes(input.value); });
          }
          say();
        });
      });
      say();
    });
  }

  // ── replyTo: "Antworte" on a reply puts a context chip above the Omniboard and focuses its field ──
  function replyTo() {
    const input = document.querySelector('[data-reply-input]');
    const ctx = document.querySelector('.reply-ctx');
    if (!input || !ctx) return;
    const label = ctx.querySelector('[data-reply-label]');
    const base = input.placeholder;
    $$('[data-reply-to]').forEach(b => b.addEventListener('click', () => {
      transition(() => { ctx.hidden = false; label.textContent = b.dataset.replyTo; input.placeholder = `${b.dataset.replyTo} …`; });
      input.focus();
    }));
    ctx.querySelector('button').addEventListener('click', () => { transition(() => { ctx.hidden = true; input.placeholder = base; }); input.focus(); });
  }

  // ── toggles: [data-toggle] buttons flip aria-pressed (save a thread, and the like) ──
  function toggles() {
    $$('[data-toggle]').forEach(b => b.addEventListener('click', () => b.setAttribute('aria-pressed', String(b.getAttribute('aria-pressed') !== 'true'))));
  }

  // ── objects: [data-life="seconds"] objects show how long they stay; data-left starts the line part-way.
  //    When the time is up they leave. [data-dismiss] lets you send one away (or hide a made-for surface). ──
  function leave(el) {
    if (!el) return;
    if (el.matches('[data-keep]')) { transition(() => { el.hidden = true; }); return; }
    if (reduce.matches) { el.remove(); return; }
    el.dataset.leaving = '';
    el.addEventListener('animationend', () => el.remove(), { once: true });
  }
  function objects() {
    $$('[data-life]').forEach(o => {
      const bar = o.querySelector('.life');
      if (!bar) return;
      const life = +o.dataset.life, left = +(o.dataset.left || life);
      bar.style.setProperty('--life', `${life}s`);
      bar.style.animationDelay = `${left - life}s`;
      bar.addEventListener('animationend', () => leave(o));
    });
    $$('[data-dismiss]').forEach(b => b.addEventListener('click', () => leave(b.closest('.object, .answer'))));
  }

  // ── ask: form[data-ask="#surface"]. Enter sends (Shift+Enter is a new line); while it works the light
  //    field moves faster; then the surface made for the question appears. A sample shows one prepared
  //    surface; a real runtime builds it from the catalog. ──
  function ask() {
    $$('form[data-ask]').forEach(form => {
      const field = form.querySelector('textarea');
      const surface = document.querySelector(form.dataset.ask);
      if (!field) return;
      field.addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); form.requestSubmit(); } });
      form.addEventListener('submit', e => {
        e.preventDefault();
        if (!field.value.trim()) return;
        field.value = '';
        field.blur();
        root.dataset.thinking = '';
        setTimeout(() => transition(() => {
          delete root.dataset.thinking;
          if (surface) { surface.hidden = false; surface.focus({ preventScroll: true }); surface.scrollIntoView({ block: 'center', behavior: reduce.matches ? 'auto' : 'smooth' }); }
        }), reduce.matches ? 0 : 1400);
      });
    });
  }

  // ── toast: a short line above the dock that says what just happened (role="status") ──
  function toast(text) {
    let t = document.querySelector('.toast');
    if (!t) { t = document.createElement('p'); t.className = 'toast lens'; t.setAttribute('role', 'status'); document.body.append(t); }
    t.textContent = text;
    t.dataset.show = '';
    clearTimeout(t._timer);
    t._timer = setTimeout(() => delete t.dataset.show, 3200);
  }

  // ── inbox: [data-inbox] holds decisions; a [data-resolve="what happened"] button folds its item into a
  //    confirmation, then the item leaves; when none are left, [data-inbox-empty] shows. ──
  function inbox() {
    $$('[data-inbox]').forEach(box => {
      const empty = document.querySelector(box.dataset.inbox);
      // [data-inbox-count="{n} offen"] says how many are left
      const check = () => {
        const n = box.querySelectorAll('.decision').length;
        if (empty) empty.hidden = n > 0;
        $$('[data-inbox-count]').forEach(el => { el.textContent = n ? el.dataset.inboxCount.replace('{n}', n) : (el.dataset.inboxNone || '0'); });
      };
      box.addEventListener('click', e => {
        const b = e.target.closest('[data-resolve]');
        if (!b) return;
        const item = b.closest('.decision');
        transition(() => {
          const done = document.createElement('p');
          done.className = 'chip confirm';
          done.setAttribute('role', 'status');
          done.innerHTML = '<svg class="icon" aria-hidden="true"><use href="' + iconHref('check') + '"/></svg>';
          done.append(document.createTextNode(b.dataset.resolve));
          item.querySelector('.decision-actions').replaceChildren(done);
        });
        setTimeout(() => { transition(() => { item.remove(); check(); }); }, reduce.matches ? 300 : 1600);
      });
      // an answer given through the Trusted Bar resolves its item too
      box.addEventListener('vv:approved', e => {
        const item = e.target.closest('.decision');
        if (item) setTimeout(() => { transition(() => { item.remove(); check(); }); }, reduce.matches ? 300 : 1600);
      });
      box.addEventListener('submit', e => {
        const f = e.target.closest('.decision form');
        if (!f) return;
        e.preventDefault();
        const field = f.querySelector('input, textarea');
        if (field && !field.value.trim()) return;
        const b = document.createElement('button');
        b.dataset.resolve = f.dataset.sent || 'Gesendet';
        b.hidden = true;
        f.append(b);
        b.click();
      });
      check();
    });
  }

  // ── tabs: [role=tablist] with [role=tab][aria-controls]; arrows move, Home/End jump ──
  function tabs() {
    $$('[role="tablist"]').forEach(list => {
      const tabs = $$('[role="tab"]', list);
      const show = tab => {
        tabs.forEach(t => {
          const on = t === tab;
          t.setAttribute('aria-selected', String(on));
          t.tabIndex = on ? 0 : -1;
          document.getElementById(t.getAttribute('aria-controls')).hidden = !on;
        });
      };
      tabs.forEach((t, i) => {
        t.addEventListener('click', () => transition(() => show(t)));
        t.addEventListener('keydown', e => {
          const k = { ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: tabs.length - 1 }[e.key];
          if (k === undefined) return;
          e.preventDefault();
          const next = tabs[(k + tabs.length) % tabs.length];
          next.focus();
          transition(() => show(next));
        });
      });
    });
  }

  // ── kanban: drag a .kcard between .kcol columns, or Alt+Arrow on a focused card. A column may hold a
  //    limit (data-limit) and say what moving there starts (data-on-drop, {id} = the card) ──
  function kanban() {
    const cols = $$('.kcol');
    if (!cols.length) return;
    const live = document.querySelector('[data-kanban-live]');
    const count = col => $$('.kcard', col).length;
    const say = text => { toast(text); if (live) live.textContent = text; };
    const move = (card, col) => {
      const from = card.closest('.kcol');
      if (!col || col === from) return;
      const limit = +col.dataset.limit || 0;
      if (limit && count(col) >= limit) { say(col.dataset.full || 'Diese Spalte ist voll.'); return; }
      transition(() => {
        col.querySelector('.kcol-cards').append(card);
        cols.forEach(c => { const n = c.querySelector('[data-count]'); if (n) n.textContent = c.dataset.limit ? `${count(c)}/${c.dataset.limit}` : count(c); });
      });
      say(col.dataset.onDrop ? col.dataset.onDrop.replace('{id}', card.dataset.id) : `${card.dataset.id} → ${col.dataset.name}`);
      card.focus();
    };
    let dragged = null;
    $$('.kcard').forEach(card => {
      card.addEventListener('dragstart', e => { dragged = card; card.dataset.dragging = ''; e.dataTransfer.effectAllowed = 'move'; e.dataTransfer.setData('text/plain', card.dataset.id); });
      card.addEventListener('dragend', () => { delete card.dataset.dragging; dragged = null; cols.forEach(c => delete c.dataset.over); });
      card.addEventListener('keydown', e => {
        if (!e.altKey || !['ArrowLeft', 'ArrowRight'].includes(e.key)) return;
        e.preventDefault();
        const i = cols.indexOf(card.closest('.kcol')) + (e.key === 'ArrowRight' ? 1 : -1);
        move(card, cols[i]);
      });
    });
    cols.forEach(col => {
      col.addEventListener('dragover', e => { if (dragged) { e.preventDefault(); col.dataset.over = ''; } });
      col.addEventListener('dragleave', () => delete col.dataset.over);
      col.addEventListener('drop', e => { e.preventDefault(); delete col.dataset.over; if (dragged) move(dragged, col); });
    });
  }

  // ── chat panel: [data-chat-toggle] opens the side panel; [data-chat-ask="question"] opens it with that
  //    question already asked (the panel answers from its [data-chat-answer] template) ──
  function chatPanel() {
    // a message to the lead: it shows at once; the sample answers from its [data-chat-reply] template
    $$('form[data-chat-form]').forEach(form => {
      const log = document.querySelector(form.dataset.chatForm);
      const field = form.querySelector('textarea, input');
      const reply = form.querySelector('template[data-chat-reply]');
      field.addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); form.requestSubmit(); } });
      form.addEventListener('submit', e => {
        e.preventDefault();
        if (!field.value.trim()) return;
        const you = document.createElement('p');
        you.className = 'msg msg-you';
        you.textContent = field.value.trim();
        field.value = '';
        transition(() => log.append(you));
        if (reply) setTimeout(() => transition(() => { log.append(reply.content.cloneNode(true)); log.scrollTop = log.scrollHeight; }), reduce.matches ? 0 : 900);
      });
    });
    const panel = document.querySelector('.chat-panel');
    if (!panel) return;
    const wide = matchMedia('(min-width: 1024px)');
    const toggles = $$('[data-chat-toggle]');
    const set = open => transition(() => {
      panel.hidden = !open;
      toggles.forEach(t => t.setAttribute('aria-expanded', String(open)));
      root.toggleAttribute('data-chat-open', open);
    });
    toggles.forEach(t => t.addEventListener('click', () => set(panel.hidden)));
    $$('[data-chat-close]', panel).forEach(b => b.addEventListener('click', () => set(false)));
    // on phones the same link goes to the chat page; on wide screens the panel opens with the question
    $$('[data-chat-ask]').forEach(b => b.addEventListener('click', e => {
      if (!wide.matches) return;
      e.preventDefault();
      const log = panel.querySelector('.chat-log');
      const answer = panel.querySelector('template[data-chat-answer]');
      transition(() => {
        panel.hidden = false;
        toggles.forEach(t => t.setAttribute('aria-expanded', 'true'));
        root.setAttribute('data-chat-open', '');
        const you = document.createElement('p');
        you.className = 'msg msg-you';
        you.textContent = b.dataset.chatAsk;
        log.append(you);
        if (answer) log.append(answer.content.cloneNode(true));
      });
    }));
  }

  // ── drafts: draft cards of a plan can be reordered (drag, or the up/down buttons) and struck out ──
  function drafts() {
    $$('[data-drafts]').forEach(list => {
      let dragged = null;
      const renumber = () => $$('.draft', list).forEach((d, i) => { const n = d.querySelector('[data-order]'); if (n) n.textContent = i + 1; });
      list.addEventListener('click', e => {
        const b = e.target.closest('[data-draft]');
        if (!b) return;
        const d = b.closest('.draft');
        transition(() => {
          if (b.dataset.draft === 'up' && d.previousElementSibling) d.previousElementSibling.before(d);
          if (b.dataset.draft === 'down' && d.nextElementSibling) d.nextElementSibling.after(d);
          if (b.dataset.draft === 'strike') { d.toggleAttribute('data-struck'); b.setAttribute('aria-pressed', String(d.hasAttribute('data-struck'))); }
          renumber();
        });
        b.focus();
      });
      $$('.draft', list).forEach(d => {
        d.addEventListener('dragstart', () => { dragged = d; d.dataset.dragging = ''; });
        d.addEventListener('dragend', () => { delete d.dataset.dragging; dragged = null; renumber(); });
        d.addEventListener('dragover', e => {
          if (!dragged || dragged === d) return;
          e.preventDefault();
          const after = e.clientY > d.getBoundingClientRect().top + d.offsetHeight / 2;
          after ? d.after(dragged) : d.before(dragged);
        });
      });
      const go = document.querySelector(list.dataset.drafts);
      if (go) go.addEventListener('click', () => {
        const n = $$('.draft:not([data-struck])', list).length;
        toast(`${n} Karten liegen jetzt in Geplant.`);
      });
    });
  }

  // icons live in one sprite next to the css/js folders; pages tell where with <html data-icons="…">
  function iconHref(id) { return `${root.dataset.icons || '../../icons.svg'}#${id}`; }

  const modules = { scroll, carousels, peek, omni, invokers, votes, bars, segFilter, approve, clock, share, states, copy, mail, hours, boardMenu, replyTo, toggles, objects, ask, inbox, tabs, kanban, chatPanel, drafts };
  window.VaievUI = modules;
  if (!root.hasAttribute('data-vaiev-manual')) Object.values(modules).forEach(m => m());
})();
