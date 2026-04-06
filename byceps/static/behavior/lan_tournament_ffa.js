/**
 * LAN Tournament – FFA Point Table Editor.
 *
 * Provides a visual drag-and-drop card-based editor for the FFA
 * point_table field on admin create/update forms.  Syncs a
 * comma-separated string of integers to a hidden <input> so form
 * submission works unchanged.
 *
 * Plain ES5 vanilla JS – no modules, no build step.
 */


/**
 * Initialise the FFA Point Table editor.
 *
 * Expects a container element with:
 *   [data-ffa-editor]           – the wrapper <div>
 *   [data-field-id]             – the id for the hidden input
 *   [data-field-name]           – the name for the hidden input
 *   [data-field-value]          – the initial value for the hidden input
 *   [data-locked]               – (optional) renders read-only cards
 *   [data-ffa-cards-container]  – the flex/grid row of point cards
 *
 * Called on DOMContentLoaded.
 */
function initPointTableEditor() {
  var wrapper = document.querySelector('[data-ffa-editor]');
  if (!wrapper) return;

  var cardsContainer = wrapper.querySelector('[data-ffa-cards-container]');
  if (!cardsContainer) return;

  var isLocked = wrapper.hasAttribute('data-locked');

  /* Create the hidden input dynamically so it doesn't shadow noscript. */
  var hiddenInput = document.createElement('input');
  hiddenInput.type = 'hidden';
  hiddenInput.id = wrapper.getAttribute('data-field-id') || '';
  hiddenInput.name = wrapper.getAttribute('data-field-name') || '';
  hiddenInput.value = wrapper.getAttribute('data-field-value') || '';
  if (isLocked) hiddenInput.disabled = true;
  wrapper.appendChild(hiddenInput);

  var addBtn = wrapper.querySelector('[data-ffa-add]');
  var presetBtns = wrapper.querySelectorAll('[data-ffa-preset]');

  /* ------------------------------------------------------------------ */
  /*  State helpers                                                      */
  /* ------------------------------------------------------------------ */

  /** Read current values from the card DOM. */
  function readValues() {
    var inputs = cardsContainer.querySelectorAll('.ffa-point-card input');
    var values = [];
    for (var i = 0; i < inputs.length; i++) {
      var v = parseInt(inputs[i].value, 10);
      values.push(isNaN(v) ? 0 : v);
    }
    return values;
  }

  /** Sync card values -> hidden input (no-op when locked). */
  function syncToHidden() {
    if (isLocked) return;
    hiddenInput.value = readValues().join(',');
  }

  /** Re-number the place badges (#1, #2, ...) after reorder. */
  function renumberCards() {
    var cards = cardsContainer.querySelectorAll('.ffa-point-card');
    for (var i = 0; i < cards.length; i++) {
      var badge = cards[i].querySelector('.ffa-point-card__place');
      if (badge) badge.textContent = '#' + (i + 1);
    }
  }

  /* ------------------------------------------------------------------ */
  /*  Card creation                                                      */
  /* ------------------------------------------------------------------ */

  /** Create a read-only card (used when the tournament is locked). */
  function createReadOnlyCard(value, index) {
    var card = document.createElement('div');
    card.className = 'ffa-point-card ffa-point-card--readonly';

    var place = document.createElement('span');
    place.className = 'ffa-point-card__place';
    place.textContent = '#' + (index + 1);

    var valSpan = document.createElement('span');
    valSpan.className = 'ffa-point-card__value';
    valSpan.textContent = String(value);

    card.appendChild(place);
    card.appendChild(valSpan);
    return card;
  }

  function createCard(value, index) {
    var card = document.createElement('div');
    card.className = 'ffa-point-card';
    card.setAttribute('draggable', 'true');

    var place = document.createElement('span');
    place.className = 'ffa-point-card__place';
    place.textContent = '#' + (index + 1);

    var input = document.createElement('input');
    input.type = 'number';
    input.className = 'ffa-point-card__value';
    input.min = '0';
    input.value = String(value);
    input.setAttribute('aria-label', 'Points for place ' + (index + 1));

    var removeBtn = document.createElement('button');
    removeBtn.type = 'button';
    removeBtn.className = 'ffa-point-card__remove';
    removeBtn.textContent = '\u00d7';
    removeBtn.title = 'Remove';
    removeBtn.setAttribute('aria-label', 'Remove place ' + (index + 1));

    card.appendChild(place);
    card.appendChild(input);
    card.appendChild(removeBtn);

    /* --- events --- */
    input.addEventListener('input', syncToHidden);

    removeBtn.addEventListener('click', function() {
      card.remove();
      renumberCards();
      syncToHidden();
    });

    /* --- drag-and-drop --- */
    card.addEventListener('dragstart', function(e) {
      card.classList.add('is-dragging');
      e.dataTransfer.effectAllowed = 'move';
      /* Store a lightweight marker – the actual reorder happens on drop. */
      e.dataTransfer.setData('text/plain', '');
    });
    card.addEventListener('dragend', function() {
      card.classList.remove('is-dragging');
    });

    return card;
  }

  /* ------------------------------------------------------------------ */
  /*  Render all cards from a values array                               */
  /* ------------------------------------------------------------------ */

  function renderCards(values) {
    cardsContainer.innerHTML = '';
    for (var i = 0; i < values.length; i++) {
      var card = isLocked
        ? createReadOnlyCard(values[i], i)
        : createCard(values[i], i);
      cardsContainer.appendChild(card);
    }
    syncToHidden();
  }

  /* ------------------------------------------------------------------ */
  /*  Drop-zone handling on the cards container                          */
  /* ------------------------------------------------------------------ */

  cardsContainer.addEventListener('dragover', function(e) {
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';

    var dragging = cardsContainer.querySelector('.is-dragging');
    if (!dragging) return;

    var afterElement = _getDragAfterElement(cardsContainer, e.clientX);
    if (afterElement == null) {
      cardsContainer.appendChild(dragging);
    } else {
      cardsContainer.insertBefore(dragging, afterElement);
    }
  });

  cardsContainer.addEventListener('drop', function(e) {
    e.preventDefault();
    renumberCards();
    syncToHidden();
  });

  /** Find the card element *after* the cursor position (horizontal). */
  function _getDragAfterElement(container, x) {
    var cards = container.querySelectorAll('.ffa-point-card:not(.is-dragging)');
    var closest = null;
    var closestOffset = Number.NEGATIVE_INFINITY;

    for (var i = 0; i < cards.length; i++) {
      var box = cards[i].getBoundingClientRect();
      var offset = x - box.left - box.width / 2;
      if (offset < 0 && offset > closestOffset) {
        closestOffset = offset;
        closest = cards[i];
      }
    }
    return closest;
  }

  /* ------------------------------------------------------------------ */
  /*  "Add place" button                                                 */
  /* ------------------------------------------------------------------ */

  if (addBtn) {
    addBtn.addEventListener('click', function() {
      var vals = readValues();
      var last = vals.length > 0 ? vals[vals.length - 1] : 1;
      var next = Math.max(last - 1, 0);
      var card = createCard(next, vals.length);
      cardsContainer.appendChild(card);
      renumberCards();
      syncToHidden();
      /* Focus the new input so user can type immediately. */
      var inp = card.querySelector('input');
      if (inp) inp.focus();
    });
  }

  /* ------------------------------------------------------------------ */
  /*  Preset buttons                                                     */
  /* ------------------------------------------------------------------ */

  if (presetBtns && presetBtns.length) {
    for (var p = 0; p < presetBtns.length; p++) {
      (function(btn) {
        btn.addEventListener('click', function() {
          var raw = btn.getAttribute('data-ffa-preset');
          var vals = raw.split(',').map(function(v) {
            return parseInt(v.trim(), 10) || 0;
          });
          renderCards(vals);
        });
      })(presetBtns[p]);
    }
  }

  /* ------------------------------------------------------------------ */
  /*  Bootstrap: parse initial value from the hidden input               */
  /* ------------------------------------------------------------------ */

  var initial = (hiddenInput.value || '').split(',').filter(function(v) {
    return v.trim() !== '';
  }).map(function(v) {
    return parseInt(v.trim(), 10) || 0;
  });

  if (initial.length > 0) {
    renderCards(initial);
  }
}


/**
 * Initialise the FFA Placement Ranking drag-and-drop editor.
 *
 * Turns a container of contestant cards into a SortableJS-powered
 * drag list.  Position in list = placement.  Cards show: drag handle,
 * ordinal (1st, 2nd ...), medal icon (top 3), contestant name,
 * live point value from pointTable, and a11y arrow buttons.
 *
 * Hidden form fields (placement_<cid>) update on every reorder so
 * the existing set_ffa_placements_action endpoint works unchanged.
 *
 * Expects the container element to have [id="ffa-ranking-list"] and
 * child .ffa-ranking-card elements with [data-contestant-id].
 *
 * Plain ES5 vanilla JS – no modules, no build step.
 *
 * @param {HTMLElement} container  – the sortable list wrapper
 * @param {number[]}    pointTable – point values by 0-based position
 */
function initFfaPlacementRanking(container, pointTable) {
  if (!container) return;
  if (!pointTable) pointTable = [];

  var MEDALS = ['\uD83E\uDD47', '\uD83E\uDD48', '\uD83E\uDD49']; /* gold, silver, bronze */

  /* ------------------------------------------------------------------ */
  /*  Ordinal helper (1st, 2nd, 3rd, 4th ...)                           */
  /* ------------------------------------------------------------------ */

  function ordinal(n) {
    var s = ['th', 'st', 'nd', 'rd'];
    var v = n % 100;
    return n + (s[(v - 20) % 10] || s[v] || s[0]);
  }

  /* ------------------------------------------------------------------ */
  /*  Update all rank-dependent displays                                 */
  /* ------------------------------------------------------------------ */

  function updateRankDisplay() {
    var cards = container.querySelectorAll('.ffa-ranking-card');
    for (var i = 0; i < cards.length; i++) {
      var card = cards[i];
      var rank = i + 1;

      /* data-rank for CSS medal borders */
      card.setAttribute('data-rank', String(rank));

      /* Ordinal badge */
      var ordEl = card.querySelector('.ffa-ranking-card__ordinal');
      if (ordEl) ordEl.textContent = ordinal(rank);

      /* Medal emoji (top 3) */
      var medalEl = card.querySelector('.ffa-ranking-card__medal');
      if (medalEl) medalEl.textContent = i < 3 ? MEDALS[i] : '';

      /* Points from pointTable, 0 when beyond table length */
      var pts = i < pointTable.length ? pointTable[i] : 0;
      var ptsEl = card.querySelector('.ffa-ranking-card__points');
      if (ptsEl) ptsEl.textContent = pts + ' pts';

      /* Hidden input for form submission */
      var hidden = card.querySelector('input[type="hidden"]');
      if (hidden) hidden.value = String(rank);

      /* Disable/enable arrow buttons at edges */
      var upBtn = card.querySelector('.ffa-ranking-card__arrow-up');
      var downBtn = card.querySelector('.ffa-ranking-card__arrow-down');
      if (upBtn) upBtn.disabled = (i === 0);
      if (downBtn) downBtn.disabled = (i === cards.length - 1);
    }
  }

  /* ------------------------------------------------------------------ */
  /*  A11y arrow buttons                                                 */
  /* ------------------------------------------------------------------ */

  function moveCard(card, direction) {
    /* direction: -1 = up, +1 = down */
    var cards = container.querySelectorAll('.ffa-ranking-card');
    var idx = -1;
    for (var i = 0; i < cards.length; i++) {
      if (cards[i] === card) { idx = i; break; }
    }
    if (idx < 0) return;

    var targetIdx = idx + direction;
    if (targetIdx < 0 || targetIdx >= cards.length) return;

    if (direction === -1) {
      container.insertBefore(card, cards[targetIdx]);
    } else {
      /* Insert after: insert before the element that follows the target */
      var after = cards[targetIdx].nextElementSibling;
      if (after) {
        container.insertBefore(card, after);
      } else {
        container.appendChild(card);
      }
    }
    updateRankDisplay();

    /* Re-focus the same button on the moved card for keyboard flow */
    var btn = direction === -1
      ? card.querySelector('.ffa-ranking-card__arrow-up')
      : card.querySelector('.ffa-ranking-card__arrow-down');
    if (btn && !btn.disabled) btn.focus();
  }

  function createA11yArrows(card) {
    var wrapper = document.createElement('div');
    wrapper.className = 'ffa-ranking-card__a11y';

    var upBtn = document.createElement('button');
    upBtn.type = 'button';
    upBtn.className = 'ffa-ranking-card__arrow-up';
    upBtn.setAttribute('aria-label', 'Move up');
    upBtn.textContent = '\u25B2';

    var downBtn = document.createElement('button');
    downBtn.type = 'button';
    downBtn.className = 'ffa-ranking-card__arrow-down';
    downBtn.setAttribute('aria-label', 'Move down');
    downBtn.textContent = '\u25BC';

    upBtn.addEventListener('click', function() { moveCard(card, -1); });
    downBtn.addEventListener('click', function() { moveCard(card, +1); });

    wrapper.appendChild(upBtn);
    wrapper.appendChild(downBtn);
    return wrapper;
  }

  /* ------------------------------------------------------------------ */
  /*  Inject a11y arrows into each card                                  */
  /* ------------------------------------------------------------------ */

  var allCards = container.querySelectorAll('.ffa-ranking-card');
  for (var c = 0; c < allCards.length; c++) {
    /* Only add arrows if not already present */
    if (!allCards[c].querySelector('.ffa-ranking-card__a11y')) {
      allCards[c].appendChild(createA11yArrows(allCards[c]));
    }
  }

  /* ------------------------------------------------------------------ */
  /*  Mismatch warning                                                   */
  /* ------------------------------------------------------------------ */

  if (pointTable.length > 0 && pointTable.length < allCards.length) {
    var warn = document.createElement('div');
    warn.className = 'ffa-mismatch-warning';
    warn.textContent = '\u26A0 Point table has ' + pointTable.length +
      ' positions but match has ' + allCards.length +
      ' contestants. Positions beyond ' + pointTable.length +
      ' will score 0 points.';
    container.parentNode.insertBefore(warn, container);
  }

  /* ------------------------------------------------------------------ */
  /*  SortableJS initialisation                                          */
  /* ------------------------------------------------------------------ */

  if (typeof Sortable !== 'undefined') {
    Sortable.create(container, {
      animation: 150,
      handle: '.ffa-ranking-card__handle',
      ghostClass: 'ffa-ranking-card--ghost',
      chosenClass: 'ffa-ranking-card--chosen',
      onEnd: function() {
        updateRankDisplay();
      }
    });
  }

  /* ------------------------------------------------------------------ */
  /*  Initial rank display                                               */
  /* ------------------------------------------------------------------ */

  updateRankDisplay();
}


/* ====================================================================
 *  Auto-init on DOM ready
 * ==================================================================== */
document.addEventListener('DOMContentLoaded', function() {
  initPointTableEditor();

  /* FFA Placement Ranking (admin match view) */
  var rankingList = document.getElementById('ffa-ranking-list');
  if (rankingList) {
    var ptRaw = rankingList.getAttribute('data-point-table') || '[]';
    var pointTable;
    try { pointTable = JSON.parse(ptRaw); } catch (e) { pointTable = []; }
    initFfaPlacementRanking(rankingList, pointTable);
  }
});
