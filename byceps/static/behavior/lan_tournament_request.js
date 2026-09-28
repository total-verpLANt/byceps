/**
 * Tournament-request form behaviour.
 *
 * Live-updates the per-head/per-unit limit input while a request form
 * is being filled in: it recomputes the cap from the team-size field
 * and a server-rendered party-capacity attribute, swaps the limit
 * field's label between its two wordings, and refreshes its caption.
 * It also keeps the elimination-mode options in sync with whichever
 * game format is currently selected in the browser: a mode invalid
 * for that format is disabled (never hidden) with its reason shown,
 * re-evaluated on every game-format change, not just at the format
 * the form happened to load or post with.
 *
 * This is convenience only. The server-side domain service is the
 * actual gate on the cap and on the format/mode combination; a POST
 * with this script disabled, or with a tampered value, is still
 * rejected there. Nothing here may become the only place either rule
 * is enforced.
 *
 * Shared, unmodified, by every surface that renders the hook
 * attributes below (site and admin forms alike). Every lookup is
 * scoped to the field's own form, so two such forms on one page
 * never interfere with each other.
 *
 * Hook attributes (rendered server-side, in the surface's own
 * markup):
 *   - the team-size input carries `data-team`;
 *   - the limit input carries `data-limit-inp`, plus
 *     `data-label-players` / `data-label-teams` (the two label
 *     strings) and, only when a party capacity is known, up to three
 *     caption templates with literal `{max}` / `{capacity}` / `{size}`
 *     tokens this script substitutes: `data-caption-template` (the
 *     per-player wording), `data-caption-template-teams` (used once
 *     the team size is above 1) and `data-caption-template-over`
 *     (used once the computed max drops below 2). Either variant may
 *     be absent on older markup, in which case the players template
 *     is used instead;
 *   - the form itself carries `data-capacity` only when a party
 *     capacity is known; its absence means no cap applies.
 *   - the form also carries `data-max-limit`, the server-side hard
 *     cap on the limit field, always present; the computed max is
 *     clamped to it either way.
 *   - a hand-written caption element may carry `data-limit-caption`;
 *     otherwise the nearest `.form-caption` in the limit input's
 *     `.form-control-block` is used, if either is present. It gets an
 *     `over` class once the max drops below 2 or the current value
 *     exceeds the max.
 *   - each elimination-mode `<input type="radio">` (site base, bote
 *     override) or `<option>` (admin `<select>`) carries
 *     `data-reasons`, a JSON object mapping every game-format value
 *     to that mode's disabled reason for it, or `null` when the mode
 *     is valid for that format;
 *   - a radio option's reason text lives in a sibling element marked
 *     `data-mode-reason`; an admin `<option>`'s own text is rewritten
 *     instead, from its `data-label-base` attribute (the plain label,
 *     no reason suffix) plus the reason.
 *   - an optional `[data-mode-hint]` element (site only; admin
 *     `<select>`s render none) carries `data-hint-template` (with a
 *     literal `{format}` token) and `data-hint-default`; its text is
 *     rewritten from the checked `game_format` radio's
 *     `data-format-label`, or restored to the default when none is
 *     checked.
 */
(function () {
  'use strict';

  function findCaption(limitInput) {
    var explicit = limitInput.form.querySelector('[data-limit-caption]');
    if (explicit) {
      return explicit;
    }

    var block = limitInput.closest('.form-control-block');
    return block ? block.querySelector('.form-caption') : null;
  }

  function update(teamSizeInput) {
    var form = teamSizeInput.form;
    if (!form) {
      return;
    }

    var limitInput = form.querySelector('[data-limit-inp]');
    if (!limitInput) {
      return;
    }

    var teamSize = parseInt(teamSizeInput.value, 10);
    var isMultiHead = teamSize > 1;

    var label = form.querySelector(
      'label[for="' + limitInput.id + '"]'
    );
    if (label) {
      var labelText = limitInput.getAttribute(
        isMultiHead ? 'data-label-teams' : 'data-label-players'
      );
      if (labelText !== null) {
        label.textContent = labelText;
      }
    }

    var maxLimit = parseInt(form.getAttribute('data-max-limit'), 10);

    if (!form.hasAttribute('data-capacity')) {
      if (isNaN(maxLimit)) {
        limitInput.removeAttribute('max');
      } else {
        limitInput.setAttribute('max', String(maxLimit));
      }
      limitInput.classList.remove('over');
      return;
    }

    var capacity = parseInt(form.getAttribute('data-capacity'), 10);
    if (isNaN(capacity) || isNaN(teamSize) || teamSize <= 0) {
      return;
    }

    var max = Math.floor(capacity / teamSize);
    if (!isNaN(maxLimit)) {
      max = Math.min(max, maxLimit);
    }
    limitInput.setAttribute('max', String(max));

    var currentValue = parseInt(limitInput.value, 10);
    limitInput.classList.toggle(
      'over',
      !isNaN(currentValue) && currentValue > max
    );

    var size = teamSize;
    var playersTemplate = limitInput.getAttribute('data-caption-template');
    var teamsTemplate = limitInput.getAttribute(
      'data-caption-template-teams'
    );
    var overTemplate = limitInput.getAttribute('data-caption-template-over');

    var template;
    if (max < 2) {
      template = overTemplate !== null ? overTemplate : playersTemplate;
    } else if (size > 1) {
      template = teamsTemplate !== null ? teamsTemplate : playersTemplate;
    } else {
      template = playersTemplate;
    }

    if (template !== null) {
      var caption = findCaption(limitInput);
      if (caption) {
        caption.textContent = template
          .split('{max}')
          .join(String(max))
          .split('{capacity}')
          .join(String(capacity))
          .split('{size}')
          .join(String(size));
        caption.classList.toggle(
          'over',
          max < 2 || (!isNaN(currentValue) && currentValue > max)
        );
      }
    }
  }

  function handleInput(event) {
    var target = event.target;
    if (!target || !target.form) {
      return;
    }

    var teamSizeInput = target.hasAttribute('data-team')
      ? target
      : target.hasAttribute('data-limit-inp')
        ? target.form.querySelector('[data-team]')
        : null;

    if (teamSizeInput) {
      update(teamSizeInput);
    }
  }

  document.addEventListener('input', handleInput);

  var teamSizeInputs = document.querySelectorAll('[data-team]');
  for (var i = 0; i < teamSizeInputs.length; i++) {
    update(teamSizeInputs[i]);
  }

  // -----------------------------------------------------------------
  // Elimination-mode options: re-evaluate after a game-format switch.
  // -----------------------------------------------------------------

  function getOwningForm(el) {
    if (el.form) {
      return el.form;
    }
    var select = el.closest('select');
    return select ? select.form : null;
  }

  function getSelectedGameFormat(form) {
    var checkedRadio = form.querySelector('[name="game_format"]:checked');
    if (checkedRadio) {
      return checkedRadio.value;
    }
    var select = form.querySelector('select[name="game_format"]');
    return select ? select.value : null;
  }

  function updateEliminationModeOptions(form) {
    var format = getSelectedGameFormat(form);
    if (!format) {
      return;
    }

    var optionEls = form.querySelectorAll('[data-reasons]');
    var firstValid = null;
    var checkedIsInvalid = false;

    for (var j = 0; j < optionEls.length; j++) {
      var optionEl = optionEls[j];
      var reasons;
      try {
        reasons = JSON.parse(optionEl.getAttribute('data-reasons'));
      } catch (e) {
        continue;
      }

      var reason = reasons[format];
      var isDisabled = reason !== null && reason !== undefined;
      var isRadio = optionEl.tagName === 'INPUT';

      optionEl.disabled = isDisabled;

      if (isRadio) {
        var card = optionEl.closest('.opt');
        if (card) {
          card.classList.toggle('is-disabled', isDisabled);
          card.classList.toggle('dis', isDisabled);
          var optLabel = card.querySelector('.opt-label');
          if (optLabel) {
            optLabel.classList.toggle('is-struck', isDisabled);
            optLabel.classList.toggle('struck', isDisabled);
          }
          var reasonEl = card.querySelector('[data-mode-reason]');
          if (reasonEl) {
            reasonEl.textContent = isDisabled ? reason : '';
            reasonEl.hidden = !isDisabled;
          }
        }
      } else {
        var labelBase = optionEl.getAttribute('data-label-base');
        if (labelBase !== null) {
          optionEl.textContent = isDisabled
            ? labelBase + ' (' + reason + ')'
            : labelBase;
        }
      }

      if (!isDisabled && firstValid === null) {
        firstValid = optionEl;
      }

      var isChecked = isRadio ? optionEl.checked : optionEl.selected;
      if (isChecked && isDisabled) {
        checkedIsInvalid = true;
      }
    }

    if (checkedIsInvalid && firstValid) {
      if (firstValid.tagName === 'INPUT') {
        var previousCard = form.querySelector('.opt.is-selected, .opt.on');
        if (previousCard) {
          previousCard.classList.remove('is-selected', 'on');
        }
        firstValid.checked = true;
        var newCard = firstValid.closest('.opt');
        if (newCard) {
          newCard.classList.add('is-selected', 'on');
        }
      } else {
        firstValid.selected = true;
        var selectEl = firstValid.closest('select');
        if (selectEl) {
          selectEl.value = firstValid.value;
        }
      }
    }
  }

  // Mode hint; a no-op without a `[data-mode-hint]` element.

  function updateModeHint(form) {
    var hint = form.querySelector('[data-mode-hint]');
    if (!hint) {
      return;
    }

    var checkedRadio = form.querySelector('[name="game_format"]:checked');
    var label = checkedRadio
      ? checkedRadio.getAttribute('data-format-label')
      : null;

    hint.textContent =
      label !== null
        ? hint.dataset.hintTemplate.split('{format}').join(label)
        : hint.dataset.hintDefault;
  }

  // Keep the is-selected/on class in sync with :checked on every user
  // click, scoped to the radio's own form, so a manual click never
  // leaves the class pointing at the option the server rendered
  // instead of the one now checked. CSS also drives the cue straight
  // off :checked (:has(input:checked)); this class only matters for
  // the server's own initial render and for browsers without :has().
  function syncSelectedCard(form, radioName, cardSelector) {
    var radios = form.querySelectorAll('input[name="' + radioName + '"]');
    for (var i = 0; i < radios.length; i++) {
      var card = radios[i].closest(cardSelector);
      if (card) {
        card.classList.toggle('is-selected', radios[i].checked);
        card.classList.toggle('on', radios[i].checked);
      }
    }
  }

  document.addEventListener('change', function (event) {
    var target = event.target;
    if (
      !target ||
      !target.form ||
      (target.name !== 'game_format' && target.name !== 'elimination_mode')
    ) {
      return;
    }

    if (target.name === 'game_format') {
      syncSelectedCard(target.form, 'game_format', '.seg-option');
      updateEliminationModeOptions(target.form);
      updateModeHint(target.form);
    } else {
      syncSelectedCard(target.form, 'elimination_mode', '.opt');
    }
  });

  var seenForms = [];
  var reasonEls = document.querySelectorAll('[data-reasons]');
  for (var k = 0; k < reasonEls.length; k++) {
    var ownForm = getOwningForm(reasonEls[k]);
    if (ownForm && seenForms.indexOf(ownForm) === -1) {
      seenForms.push(ownForm);
      updateEliminationModeOptions(ownForm);
    }
  }

  var hintForms = [];
  var hintEls = document.querySelectorAll('[data-mode-hint]');
  for (var h = 0; h < hintEls.length; h++) {
    var hintForm = hintEls[h].closest('form');
    if (hintForm && hintForms.indexOf(hintForm) === -1) {
      hintForms.push(hintForm);
      updateModeHint(hintForm);
    }
  }
})();
