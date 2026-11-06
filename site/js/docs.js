/* Supplement site.js's single current-section controller. Native links and details
   work without JavaScript; no IntersectionObserver or network access is needed. */
(() => {
  const nav = document.querySelector('.docs-nav');
  if (!nav) return;
  // site.js owns aria-current and scroll position. Ask it to remeasure after
  // history navigation or a disclosure changes the document's geometry.
  const refresh = () => dispatchEvent(new Event('scroll'));
  let queued = false;
  function keepCurrentVisible() {
    queued = false;
    if (nav.scrollHeight <= nav.clientHeight + 2 || nav.contains(document.activeElement)) return;
    const current = nav.querySelector('a[aria-current="location"]');
    if (!current) return;
    const box = nav.getBoundingClientRect(), item = current.getBoundingClientRect();
    if (item.top < box.top) nav.scrollTop += item.top - box.top;
    else if (item.bottom > box.bottom) nav.scrollTop += item.bottom - box.bottom;
  }
  function scheduleVisibility() {
    if (!queued) { queued = true; requestAnimationFrame(keepCurrentVisible); }
  }
  function revealHash() {
    let id;
    try { id = decodeURIComponent(location.hash.slice(1)); } catch (_) { return; }
    const target = document.getElementById(id);
    if (target) {
      let parent = target.parentElement, opened = false;
      while (parent) {
        if (parent.tagName === 'DETAILS' && !parent.open) { parent.open = true; opened = true; }
        parent = parent.parentElement;
      }
      if (opened) target.scrollIntoView({ block: 'start', behavior: 'instant' });
    }
    requestAnimationFrame(refresh);
  }
  document.querySelectorAll('.docs-details').forEach(detail => detail.addEventListener('toggle', refresh));
  addEventListener('hashchange', revealHash);
  addEventListener('popstate', () => requestAnimationFrame(revealHash));
  addEventListener('pageshow', revealHash);
  addEventListener('load', revealHash);
  addEventListener('scroll', scheduleVisibility, { passive: true });
  addEventListener('resize', scheduleVisibility);
  revealHash();
})();
