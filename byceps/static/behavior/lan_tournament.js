/**
 * LAN Tournament – bracket view-switcher and drag-to-pan.
 *
 * Extracted from Turniercss/turnier-shared.js, adapted for
 * server-rendered bracket HTML (Jinja2 macros).
 *
 * Plain ES5 vanilla JS – no modules, no build step.
 */


/**
 * View-mode switcher for bracket rounds and DE section filtering.
 *
 * Operates on `.view-select` elements within a `.bracket-shell`.
 *
 * Modes:
 *   "full"     – show every round and every bracket section (default).
 *   "top8"     – hide all rounds except the last 3 (QF, SF, F).
 *   "winners"  – show only sections with data-bracket-view="winners"
 *                or "finals"; hide "losers".
 *   "losers"   – show only sections with data-bracket-view="losers"
 *                or "finals"; hide "winners".
 */
function initBracketViewSwitcher() {
  var selects = document.querySelectorAll('.view-select');
  selects.forEach(function(select) {
    var shell = select.closest('.bracket-shell');
    if (!shell) return;

    // Guard against double-binding on re-init after client-side render
    if (select.getAttribute('data-view-bound')) return;
    select.setAttribute('data-view-bound', '1');

    function applyMode(mode) {
      // --- Round-level filtering (server-rendered brackets) ---
      var rounds = shell.querySelectorAll('.bracket-round');
      var total = rounds.length;
      rounds.forEach(function(round, i) {
        if (mode === 'top8') {
          round.style.display = (i >= total - 3) ? '' : 'none';
        } else {
          round.style.display = '';
        }
      });

      // --- Section-level filtering (client-rendered DE brackets) ---
      var sections = shell.querySelectorAll('.lt-bracket-section[data-bracket-view]');
      if (sections.length === 0) return;

      sections.forEach(function(section) {
        var view = section.getAttribute('data-bracket-view');
        if (mode === 'winners') {
          // Show winners and finals, hide losers
          section.style.display = (view === 'losers') ? 'none' : '';
        } else if (mode === 'losers') {
          // Show losers and finals, hide winners
          section.style.display = (view === 'winners') ? 'none' : '';
        } else {
          // 'full' or any other mode: show all
          section.style.display = '';
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
 * "My Matches" filter for the match list page.
 *
 * Toggles visibility of match rows based on whether the current
 * user's team or participant ID appears in the row's data attributes.
 * State persists via ?my_matches=1 URL query param.
 */
function initMatchFilter() {
  var btn = document.getElementById('match-filter-mine');
  if (!btn) return;

  var userTeamId = btn.getAttribute('data-user-team-id') || '';
  var userParticipantId = btn.getAttribute('data-user-participant-id') || '';
  var active = false;

  // Restore state from URL query param on page load
  var params = new URLSearchParams(window.location.search);
  if (params.get('my_matches') === '1') {
    active = true;
    btn.setAttribute('aria-pressed', 'true');
    btn.classList.add('active');
  }

  function applyFilter() {
    var rows = document.querySelectorAll('.match-link-reset');
    rows.forEach(function(row) {
      if (!active) {
        row.classList.remove('match-row--hidden');
        return;
      }
      var teamIds = (row.getAttribute('data-team-ids') || '').split(',');
      var participantIds = (row.getAttribute('data-participant-ids') || '').split(',');
      // Hide matches without real contestants (TBD/defwin slots)
      var hasContestants = (row.getAttribute('data-team-ids') || '').replace(/,/g, '') !== ''
                        || (row.getAttribute('data-participant-ids') || '').replace(/,/g, '') !== '';
      if (!hasContestants) {
        row.classList.add('match-row--hidden');
        return;
      }
      var isMyMatch = (userTeamId && teamIds.indexOf(userTeamId) !== -1)
                   || (userParticipantId && participantIds.indexOf(userParticipantId) !== -1);
      row.classList.toggle('match-row--hidden', !isMyMatch);
    });

    // Persist state to URL without page reload
    var url = new URL(window.location);
    if (active) {
      url.searchParams.set('my_matches', '1');
    } else {
      url.searchParams.delete('my_matches');
    }
    history.replaceState(null, '', url);
  }

  btn.addEventListener('click', function() {
    active = !active;
    btn.setAttribute('aria-pressed', String(active));
    btn.classList.toggle('active', active);
    applyFilter();
  });

  // Apply on load if restored from URL
  if (active) applyFilter();
}


/**
 * Init on DOM ready + debounced resize handler for pannable state.
 */
document.addEventListener('DOMContentLoaded', function() {
  initBracketViewSwitcher();
  initMatchFilter();

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
