/* gv-home.js: homepage countdown.
 * Reads .countdown[data-start] (ISO 8601, UTC) and keeps the three .cd-unit b
 * digits (days, hours, minutes) current. The group's aria-label carries the
 * absolute date and is never rewritten; the digits are aria-hidden. */
(function () {
  'use strict';

  var reduce = window.matchMedia('(prefers-reduced-motion: reduce)');
  var timer = 0;

  // A value without zone designator is read as UTC, not as local time.
  function parseStart(s) {
    s = String(s || '').trim();
    if (/T/.test(s) && !/(Z|[+-]\d\d(:?\d\d)?)$/i.test(s)) s += 'Z';
    return Date.parse(s);
  }

  var items = Array.from(document.querySelectorAll('.countdown[data-start]'), function (el) {
    return { el: el, start: parseStart(el.getAttribute('data-start')) };
  }).filter(function (c) { return !isNaN(c.start); });
  if (!items.length) return;

  function pad(n) { return n < 10 ? '0' + n : String(n); }

  function render(c, now) {
    var left = c.start - now;
    var vals = [Math.floor(left / 864e5), Math.floor(left % 864e5 / 36e5), Math.floor(left % 36e5 / 6e4)];
    var digits = c.el.querySelectorAll('.cd-unit b');
    for (var i = 0; i < digits.length && i < vals.length; i++) digits[i].textContent = pad(vals[i]);
  }

  function stop() { clearInterval(timer); timer = 0; }

  function tick() {
    var now = Date.now();
    items = items.filter(function (c) {
      if (c.start <= now) { c.el.hidden = true; c.el.style.display = 'none'; return false; }
      render(c, now);
      return true;
    });
    if (!items.length) stop();
  }

  // Runs while the tab is visible: every 30 s, or once per minute with reduced motion.
  function run() {
    stop();
    if (document.hidden) return;
    tick();
    if (items.length) timer = setInterval(tick, reduce.matches ? 60000 : 30000);
  }

  document.addEventListener('visibilitychange', run);
  if (reduce.addEventListener) reduce.addEventListener('change', run);
  run();
})();
