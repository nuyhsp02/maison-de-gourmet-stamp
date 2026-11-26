'use strict';
// Both production pages start hidden. The server supplied this lifetime for this navigation.
(() => {
  const budget = Number(document.currentScript.dataset.remainingMs);
  const start = performance.now();
  const wallStart = Date.now();
  let timer, expired = false;
  function check() {
    if (expired) return;
    clearTimeout(timer);
    // The monotonic clock resists wall-clock changes. The second clock also counts sleep.
    const elapsed = Math.max(performance.now(), start + Math.max(0, Date.now() - wallStart));
    const left = budget - elapsed;
    if (!Number.isSafeInteger(budget) || left <= 0) {
      expired = true;
      document.body.hidden = true;
      for (const dialog of document.querySelectorAll('dialog[open]')) dialog.close();
      document.body.replaceChildren();
      location.replace('/');
      return;
    }
    timer = setTimeout(check, Math.min(left, 2_147_000_000));
  }
  check();
  if (!expired) document.body.hidden = false;
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) check();
  });
  window.addEventListener('focus', check);
  window.addEventListener('pagehide', () => { document.documentElement.hidden = true; });
  window.addEventListener('pageshow', event => {
    if (event.persisted) location.reload();
  });
})();
