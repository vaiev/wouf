/* wouf.dev — examples.html only. The dialogs are the site's demo (js/site.js plays every [data-demo]); this only
   marks, in the jump row, the example being read (aria-current="location"): the last one whose top has passed
   the line under the bar and the row, the first before that, the last at the very bottom. No inline styles. */
(() => {
  const nav = document.querySelector('.example-nav');
  if (!nav) return;
  const LINE = 200;
  const items = [...nav.querySelectorAll('a')]
    .map(a => ({ a, at: document.getElementById((a.getAttribute('href') || '').slice(1)) }))
    .filter(item => item.at);
  if (!items.length) return;
  let current = null, queued = false;
  function mark() {
    queued = false;
    let at = items[0];
    for (const item of items) if (item.at.getBoundingClientRect().top <= LINE + 1) at = item;
    if (scrollY + innerHeight >= document.documentElement.scrollHeight - 2) at = items[items.length - 1];
    if (at === current) return;
    if (current) current.a.removeAttribute('aria-current');
    at.a.setAttribute('aria-current', 'location');
    current = at;
  }
  function schedule() { if (!queued) { queued = true; requestAnimationFrame(mark); } }
  addEventListener('scroll', schedule, { passive: true });
  addEventListener('resize', schedule);
  addEventListener('load', schedule);
  mark();
})();
