/* Der Rentenbote — Tages-/Spätausgabe (light/dark) toggle.
   The pre-paint script in base.html <head> sets html[data-theme] before first
   paint (from localStorage 'bote-theme', else the OS preference). This file
   only wires the toggle button and keeps its pressed-state in sync; the label
   text itself is static markup, shown/hidden purely by CSS on html[data-theme]
   so the button keeps a constant width across both editions. */
(function () {
  'use strict';

  function currentTheme() {
    return document.documentElement.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';
  }

  function syncLabel(btn) {
    btn.setAttribute('aria-pressed', currentTheme() === 'dark' ? 'true' : 'false');
  }

  function setTheme(next) {
    document.documentElement.setAttribute('data-theme', next);
    try { localStorage.setItem('bote-theme', next); } catch (e) {}
  }

  function init() {
    var toggles = document.querySelectorAll('[data-theme-toggle]');
    toggles.forEach(function (btn) {
      syncLabel(btn);
      btn.addEventListener('click', function () {
        setTheme(currentTheme() === 'dark' ? 'light' : 'dark');
        toggles.forEach(syncLabel);
      });
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
