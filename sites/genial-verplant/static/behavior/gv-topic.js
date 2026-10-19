/* gv-topic.js: one-time reveal moment for the party topic.
 * The server renders every topic slot. This script only sets the class
 * tp-reveal on <html> for a moment, once per party and browser, and only
 * while the topic is announced. Vanilla JS, no dependencies. */
(function () {
  'use strict';

  var root = document.documentElement;
  if (root.getAttribute('data-topic-state') !== 'on') return;
  if (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;

  var key = 'tv-topic-seen-' + (root.getAttribute('data-party-id') || '');
  var seen;
  try { seen = localStorage.getItem(key); } catch (e) { return; }
  if (seen !== null) return;

  root.classList.add('tp-reveal');
  setTimeout(function () { root.classList.remove('tp-reveal'); }, 1400);
  try { localStorage.setItem(key, '1'); } catch (e) {}
})();
