/**
 * LAN Tournament – bracket view-switcher and drag-to-pan.
 *
 * Extracted from Turniercss/turnier-shared.js, adapted for
 * server-rendered bracket HTML (Jinja2 macros).
 *
 * Plain ES5 vanilla JS – no modules, no build step.
 */


/**
 * View-mode switcher for bracket rounds.
 *
 * Operates on `.view-select` elements within a `.bracket-shell`.
 * "top8" hides all rounds except the last 3 (QF, SF, F).
 * "full" shows every round.
 */
function initBracketViewSwitcher() {
  var selects = document.querySelectorAll('.view-select');
  selects.forEach(function(select) {
    var shell = select.closest('.bracket-shell');
    if (!shell) return;

    function applyMode(mode) {
      var rounds = shell.querySelectorAll('.bracket-round');
      var total = rounds.length;
      rounds.forEach(function(round, i) {
        if (mode === 'top8') {
          round.style.display = (i >= total - 3) ? '' : 'none';
        } else {
          round.style.display = '';
        }
      });
    }

    select.addEventListener('change', function() {
      applyMode(select.value);
    });

    applyMode(select.value || 'full');
  });
}


/**
 * Bracket drag-to-pan on desktop.
 *
 * Targets a `.bracket-desktop` scroll wrapper. Skipped on touch
 * devices so native scroll/swipe works undisturbed.
 *
 * Adds `.is-pannable` when content overflows, `.is-panning` while
 * the user is actively dragging. Interactive children (links,
 * buttons, inputs) are excluded from capture.
 */
function enableBracketPan(scrollEl) {
  var canvas = scrollEl.querySelector('.bracket');
  if (!canvas) return;

  var needsPan = canvas.scrollWidth > (scrollEl.clientWidth + 8);
  var useNativeTouchPan = window.matchMedia('(hover: none), (pointer: coarse)').matches;
  scrollEl.classList.toggle('is-pannable', needsPan);

  if (!needsPan) return;
  if (useNativeTouchPan) return;

  var interactiveSelector = 'a, button, select, input, label';
  var pointerId = null;
  var startX = 0;
  var startScrollLeft = 0;
  var moved = false;

  scrollEl.addEventListener('pointerdown', function(event) {
    if (event.button !== 0) return;
    if (event.target.closest && event.target.closest(interactiveSelector)) return;

    pointerId = event.pointerId;
    moved = false;
    startX = event.clientX;
    startScrollLeft = scrollEl.scrollLeft;
    scrollEl.classList.add('is-panning');
    scrollEl.setPointerCapture(pointerId);
  });

  scrollEl.addEventListener('pointermove', function(event) {
    if (pointerId !== event.pointerId) return;
    var delta = event.clientX - startX;
    if (Math.abs(delta) > 3) moved = true;
    scrollEl.scrollLeft = startScrollLeft - delta;
  });

  function stopPan(event) {
    if (pointerId !== event.pointerId) return;
    scrollEl.classList.remove('is-panning');
    if (scrollEl.hasPointerCapture(pointerId)) {
      scrollEl.releasePointerCapture(pointerId);
    }
    if (moved) {
      event.preventDefault();
    }
    pointerId = null;
  }

  scrollEl.addEventListener('pointerup', stopPan);
  scrollEl.addEventListener('pointercancel', stopPan);
  scrollEl.addEventListener('lostpointercapture', function() {
    pointerId = null;
    scrollEl.classList.remove('is-panning');
  });
}


/**
 * Init on DOM ready + debounced resize handler for pannable state.
 */
document.addEventListener('DOMContentLoaded', function() {
  initBracketViewSwitcher();

  document.querySelectorAll('.bracket-desktop').forEach(enableBracketPan);

  // Enable pan on new-style brackets (client-side renderer)
  document.querySelectorAll('.lt-bracket-scroll').forEach(enableBracketPan);

  var resizeTimer;
  window.addEventListener('resize', function() {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(function() {
      var selectors = '.bracket-desktop, .lt-bracket-scroll';
      document.querySelectorAll(selectors).forEach(function(el) {
        var canvas = el.querySelector('.bracket, .lt-bracket-canvas');
        if (canvas) {
          el.classList.toggle('is-pannable', canvas.scrollWidth > el.clientWidth + 8);
        }
      });
    }, 110);
  });
});
