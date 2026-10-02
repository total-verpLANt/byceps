/*
 * Seeding board: swap slots and reorder tiers by dragging or by tapping.
 *
 * The script only proposes actions. Every change is posted to the seeding
 * action route (`Accept: application/json`) and the page is then rendered
 * again from the server, so the seed code and the layout are never computed
 * here. All texts come from the board's `strings`, rendered on the server.
 *
 * `initOrderingBoard` lifts the forms of the qualification page (tie
 * ordering, reasons, confirm dialogs) for both orga surfaces. Its forms post
 * natively, so the server answers with a redirect and a flash; without the
 * script they still work.
 *
 * Node: `module.exports` (the pure helpers). Browser: the global `LtSeeding`.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) {
    module.exports = factory();
  } else {
    root.LtSeeding = factory();
    if (typeof document !== 'undefined') {
      var start = function () {
        root.LtSeeding.init(document);
      };
      if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', start);
      } else {
        start();
      }
    }
  }
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  // ------------------------------------------------------------------ //
  // pure helpers

  function fill(template, params) {
    return String(template).replace(/%\((\w+)\)s/g, function (whole, key) {
      return Object.prototype.hasOwnProperty.call(params || {}, key)
        ? String(params[key])
        : whole;
    });
  }

  // Next selection state for a tap, a cancel or a finished drop.
  // `state` is `{selected: key | null}`; `event` is `{type: 'pick', key}`,
  // `{type: 'cancel'}` or `{type: 'done'}`. The effect tells the caller what
  // to show or which action to propose.
  function nextSelection(state, event) {
    var selected = state && state.selected != null ? state.selected : null;
    if (event.type === 'pick') {
      if (selected === null) {
        return {
          selected: event.key,
          effect: { type: 'selected', key: event.key }
        };
      }
      if (selected === event.key) {
        return { selected: null, effect: { type: 'cancelled', key: selected } };
      }
      return {
        selected: null,
        effect: { type: 'apply', from: selected, to: event.key }
      };
    }
    if (event.type === 'cancel') {
      if (selected === null) {
        return { selected: null, effect: { type: 'none' } };
      }
      return { selected: null, effect: { type: 'cancelled', key: selected } };
    }
    if (event.type === 'done') {
      return { selected: null, effect: { type: 'none' } };
    }
    return { selected: selected, effect: { type: 'none' } };
  }

  function findSlot(board, index) {
    var layout = board.layout;
    var i;
    var j;
    if (layout.kind === 'bracket') {
      for (i = 0; i < layout.matches.length; i++) {
        for (j = 0; j < layout.matches[i].slots.length; j++) {
          if (layout.matches[i].slots[j].index === index) {
            return { slot: layout.matches[i].slots[j], match: layout.matches[i] };
          }
        }
      }
    } else {
      for (i = 0; i < layout.groups.length; i++) {
        for (j = 0; j < layout.groups[i].slots.length; j++) {
          if (layout.groups[i].slots[j].index === index) {
            return { slot: layout.groups[i].slots[j], group: layout.groups[i] };
          }
        }
      }
    }
    return null;
  }

  function slotDescription(board, index, strings) {
    var found = findSlot(board, index);
    if (found === null) return null;
    var name = found.slot.bye ? strings.bye : found.slot.name;
    var where;
    if (found.match) {
      where = fill(strings.where_match, {
        n: found.match.number,
        pos: found.slot.seed_position
      });
    } else if (board.format === 'FFA') {
      where = fill(strings.lobby, { n: found.group.index + 1 });
    } else {
      where = fill(strings.group, { letter: found.group.letter });
    }
    return { name: name, where: where };
  }

  // The announcement for a swap of the slots `p` and `q` (board before it).
  function describeSwap(board, p, q, strings) {
    var a = slotDescription(board, p, strings);
    var b = slotDescription(board, q, strings);
    if (a === null || b === null) return '';
    return fill(strings.swapped, {
      a: a.name,
      wa: a.where,
      b: b.name,
      wb: b.where
    });
  }

  function layoutSlots(board) {
    var layout = board.layout;
    var slots = [];
    var groups = layout.kind === 'bracket' ? layout.matches : layout.groups;
    groups.forEach(function (part) {
      part.slots.forEach(function (slot) {
        slots[slot.index] = slot;
      });
    });
    return slots;
  }

  // The name pairs whose slots traded places between two boards.
  function separationPairs(before, after) {
    var was = layoutSlots(before);
    var now = layoutSlots(after);
    var used = {};
    var pairs = [];
    var i;
    var j;
    for (i = 0; i < was.length; i++) {
      if (used[i] || !now[i] || was[i].contestant_id === now[i].contestant_id) {
        continue;
      }
      for (j = i + 1; j < was.length; j++) {
        if (
          !used[j] &&
          now[j] &&
          now[j].contestant_id === was[i].contestant_id &&
          was[j].contestant_id === now[i].contestant_id
        ) {
          used[i] = true;
          used[j] = true;
          pairs.push([was[i].name, was[j].name]);
          break;
        }
      }
    }
    return pairs;
  }

  // The notice after "separate same-group pairings" (boards before, after).
  function describeSeparation(before, after, strings) {
    var pairs = separationPairs(before, after)
      .map(function (pair) {
        return pair[0] + ' \u2194 ' + pair[1];
      })
      .join(', ');
    return fill(strings.separated, { pairs: pairs });
  }

  function findContestant(board, contestantId) {
    var i;
    var j;
    for (i = 0; i < board.tiers.length; i++) {
      for (j = 0; j < board.tiers[i].contestants.length; j++) {
        if (board.tiers[i].contestants[j].id === contestantId) {
          return {
            tier: board.tiers[i],
            contestant: board.tiers[i].contestants[j],
            position: j,
            count: board.tiers[i].contestants.length
          };
        }
      }
    }
    return null;
  }

  // The announcement for a finished tier move (board after it).
  function describeMove(board, contestantId, strings, hadFixes) {
    var found = findContestant(board, contestantId);
    if (found === null) return '';
    return fill(hadFixes ? strings.moved_reset : strings.moved, {
      name: found.contestant.name,
      letter: found.tier.letter,
      n: found.contestant.seed
    });
  }

  // Form fields for moving `contestantId` to `tierIndex`, before `refId`
  // (`null` = at the end of the tier).
  function moveFields(contestantId, tierIndex, refId) {
    var fields = {
      action: 'move_tier',
      contestant_id: contestantId,
      tier: String(tierIndex)
    };
    if (refId) fields.ref_id = refId;
    return fields;
  }

  // Where the up/down buttons send a contestant, or `null` at the edges.
  function stepTarget(board, contestantId, direction) {
    var found = findContestant(board, contestantId);
    if (found === null) return null;
    var list = found.tier.contestants;
    var at = found.position;
    if (direction === 'up') {
      if (at === 0) return null;
      return { tier: found.tier.index, ref_id: list[at - 1].id };
    }
    if (at === list.length - 1) return null;
    var after = list[at + 2];
    return { tier: found.tier.index, ref_id: after ? after.id : null };
  }

  function fixesText(count, strings) {
    if (!count) return strings.fixes_none;
    if (count === 1) return strings.fixes_one;
    return fill(strings.fixes_many, { n: count });
  }

  // The message after a successful replay (board after it).
  function describeReplay(board, strings) {
    return fill(strings.replayed, {
      format: board.format_name,
      n: board.player_count,
      fixes: fixesText(board.fix_count, strings)
    });
  }

  // Which notice a tier change needs: its fixes were dropped, or not.
  function tierNoticeKind(boardBefore) {
    return boardBefore.fix_count > 0 ? 'notice_reset' : 'notice_rebuilt';
  }

  // Who moved and where (boards before and after); empty when unknown.
  function describeTierMove(before, after, contestantId, strings) {
    if (!contestantId) return '';
    var from = findContestant(before, contestantId);
    var to = findContestant(after, contestantId);
    if (from === null || to === null) return '';
    if (from.tier.index === to.tier.index) {
      return fill(strings.notice_reorder, {
        name: to.contestant.name,
        place: to.contestant.seed,
        tier: to.tier.letter
      });
    }
    return fill(strings.notice_move, {
      name: to.contestant.name,
      from: from.tier.letter,
      to: to.tier.letter,
      place: to.contestant.seed
    });
  }

  // A dragged move already sits in the DOM; after a 409, or when it was
  // dropped while a request ran, the server never saw it.
  function dropNeedsRevert(options, reason) {
    return (
      !!options && options.drag === true &&
      (reason === 'conflict' || reason === 'busy')
    );
  }

  // Let a form post once: true on its first call, false afterwards.
  function firstSubmit(form) {
    if (form.getAttribute('data-lt-sent') === '1') return false;
    form.setAttribute('data-lt-sent', '1');
    return true;
  }

  // Route one submit inside the board. Returns true when the browser
  // may post the form itself: a plain form, on its first submit only.
  function routeSubmit(form, isAction, hooks) {
    var title = form.getAttribute('data-confirm-title');
    if (!isAction && title === null) return firstSubmit(form);
    var go = function () {
      if (isAction) {
        hooks.perform();
      } else if (firstSubmit(form)) {
        hooks.post();
      }
    };
    if (title !== null) {
      hooks.confirm(title, go);
    } else {
      go();
    }
    return false;
  }

  // A tie is ordered by moving a name up or down. `ids` is the order now.
  function moveInOrder(ids, index, direction) {
    var target = direction === 'up' ? index - 1 : index + 1;
    if (index < 0 || index >= ids.length || target < 0 || target >= ids.length) {
      return { ids: ids.slice(), index: index, moved: false };
    }
    var next = ids.slice();
    var held = next[index];
    next[index] = next[target];
    next[target] = held;
    return { ids: next, index: target, moved: true };
  }

  function describeOrderMove(name, firstPlace, index, strings) {
    return fill(strings.order_moved || '', {
      name: name,
      place: firstPlace + index
    });
  }

  // The save button of a decision stays off until a reason is written.
  function reasonFilled(text) {
    return typeof text === 'string' && text.trim() !== '';
  }

  function dialogLines(body) {
    if (!body) return [];
    return String(body)
      .split('\n')
      .map(function (line) {
        return line.trim();
      })
      .filter(function (line) {
        return line !== '';
      });
  }

  // ------------------------------------------------------------------ //
  // browser part

  function readBoard(rootEl) {
    var script = rootEl.querySelector('script[data-lt-seed-board]');
    if (script === null) return null;
    try {
      return JSON.parse(script.textContent);
    } catch (e) {
      return null;
    }
  }

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  function encode(fields) {
    var parts = [];
    Object.keys(fields).forEach(function (key) {
      parts.push(
        encodeURIComponent(key) + '=' + encodeURIComponent(fields[key])
      );
    });
    return parts.join('&');
  }

  function openDialog(config) {
    var previous = document.activeElement;
    var overlay = el('div', 'lt-seed-ov');
    var dialog = el(
      'div',
      'lt-seed-dlg ' + (config.danger ? 'is-danger' : 'is-warn')
    );
    dialog.setAttribute('role', 'dialog');
    dialog.setAttribute('aria-modal', 'true');
    dialog.setAttribute('aria-label', config.title);
    dialog.appendChild(el('h3', null, config.title));
    var lines = dialogLines(config.body);
    if (lines.length === 1) {
      dialog.appendChild(el('p', null, lines[0]));
    } else if (lines.length > 1) {
      var list = el('ul');
      lines.forEach(function (line) {
        list.appendChild(el('li', null, line));
      });
      dialog.appendChild(list);
    }
    var row = el('div', 'lt-seed-row lt-seed-end');
    var cancel = el('button', 'button', config.cancelLabel);
    cancel.type = 'button';
    var ok = el(
      'button',
      'button ' + (config.danger ? 'color-danger' : 'color-primary'),
      config.okLabel
    );
    ok.type = 'button';
    row.appendChild(cancel);
    row.appendChild(ok);
    dialog.appendChild(row);
    overlay.appendChild(dialog);
    document.body.appendChild(overlay);

    function close() {
      document.removeEventListener('keydown', onKey, true);
      document.body.removeChild(overlay);
      if (previous && previous.focus) previous.focus();
    }
    function onKey(event) {
      if (event.key === 'Escape') {
        event.preventDefault();
        close();
      } else if (event.key === 'Tab') {
        event.preventDefault();
        (document.activeElement === cancel ? ok : cancel).focus();
      }
    }
    cancel.addEventListener('click', close);
    ok.addEventListener('click', function () {
      close();
      config.onOk();
    });
    document.addEventListener('keydown', onKey, true);
    cancel.focus();
  }

  function initBoard(rootEl) {
    if (rootEl.getAttribute('data-lt-seed-ready')) return;
    rootEl.setAttribute('data-lt-seed-ready', '1');
    rootEl.classList.add('is-js');

    var live = el('div', 'lt-seed-sr');
    live.setAttribute('aria-live', 'polite');
    rootEl.parentNode.insertBefore(live, rootEl.nextSibling);

    var state = {
      board: null,
      strings: {},
      selection: { selected: null },
      selectionMode: null,
      tab: null,
      busy: false,
      stale: false,
      sortables: [],
      last: null
    };

    function announce(text) {
      if (!text) return;
      live.textContent = '';
      setTimeout(function () {
        live.textContent = text;
      }, 30);
    }

    function str(key, params) {
      return fill(state.strings[key] || '', params);
    }

    // -------------------------------------------------------------- //
    // save indicator, banners

    function setSave(kind) {
      var node = rootEl.querySelector('[data-lt-seed-save]');
      if (node === null) return;
      node.className = 'lt-seed-save s-' + kind;
      node.textContent = '';
      var icon = el('i');
      icon.setAttribute('aria-hidden', 'true');
      if (kind === 'busy') {
        icon.className = 'lt-seed-spin';
        node.appendChild(icon);
        node.appendChild(document.createTextNode(str('saving')));
      } else if (kind === 'ok') {
        icon.textContent = '\u2713';
        node.appendChild(icon);
        node.appendChild(document.createTextNode(str('saved')));
      } else {
        icon.textContent = '\u2715';
        node.appendChild(icon);
        var retry = el('button', 'lt-seed-link', str('not_saved'));
        retry.type = 'button';
        retry.setAttribute('data-retry', '1');
        node.appendChild(retry);
      }
    }

    function banner(kind, title, body, actions) {
      var glyphs = { ok: '\u2713', warn: '\u25B2', err: '\u2715', info: 'i' };
      var box = el('div', 'lt-seed-nt nt-' + kind);
      box.setAttribute('role', kind === 'err' ? 'alert' : 'status');
      var head = el('p', 'lt-seed-nth');
      var icon = el('i', null, glyphs[kind]);
      icon.setAttribute('aria-hidden', 'true');
      head.appendChild(icon);
      head.appendChild(el('span', null, title));
      box.appendChild(head);
      if (body) {
        var inner = el('div', 'lt-seed-ntb');
        inner.appendChild(el('p', null, body));
        box.appendChild(inner);
      }
      if (actions && actions.length) {
        var row = el('div', 'lt-seed-row');
        actions.forEach(function (action) {
          row.appendChild(action);
        });
        box.appendChild(row);
      }
      return box;
    }

    function showBanner(node) {
      var area = rootEl.querySelector('[data-lt-seed-banners]');
      if (area === null) return;
      var existing = area.querySelectorAll('[data-lt-seed-transient]');
      for (var i = 0; i < existing.length; i++) {
        area.removeChild(existing[i]);
      }
      node.setAttribute('data-lt-seed-transient', '1');
      area.insertBefore(node, area.firstChild);
    }

    function actionButton(label, attribute) {
      var button = el('button', 'button is-compact', label);
      button.type = 'button';
      button.setAttribute(attribute, '1');
      return button;
    }

    function showConflict(message) {
      showBanner(
        banner(
          'warn',
          message,
          str('conflict_body'),
          [actionButton(str('reload'), 'data-reload')]
        )
      );
      setSave('err');
    }

    // -------------------------------------------------------------- //
    // selection (tap to choose)

    function isLocked() {
      return state.board.generation === 'locked';
    }

    function clearSelectionMarks() {
      var marked = rootEl.querySelectorAll('.is-sel, .is-tgt');
      for (var i = 0; i < marked.length; i++) {
        marked[i].classList.remove('is-sel');
        marked[i].classList.remove('is-tgt');
      }
      var pressed = rootEl.querySelectorAll('.lt-seed-cb[aria-pressed="true"]');
      for (var j = 0; j < pressed.length; j++) {
        pressed[j].setAttribute('aria-pressed', 'false');
      }
      var ends = rootEl.querySelectorAll('.lt-seed-tend');
      for (var k = 0; k < ends.length; k++) {
        ends[k].parentNode.removeChild(ends[k]);
      }
      var hint = rootEl.querySelector('.lt-seed-hintbar');
      if (hint !== null) hint.parentNode.removeChild(hint);
    }

    function buttonFor(mode, key) {
      var selector =
        mode === 'tier'
          ? '.lt-seed-tchip[data-id="' + cssEscape(key) + '"] .lt-seed-cb'
          : '.lt-seed-chip[data-i="' + key + '"] .lt-seed-cb';
      return rootEl.querySelector(selector);
    }

    function cssEscape(value) {
      return String(value).replace(/["\\]/g, '\\$&');
    }

    function nameOf(mode, key) {
      if (mode === 'tier') {
        var found = findContestant(state.board, key);
        return found === null ? '' : found.contestant.name;
      }
      var slot = slotDescription(state.board, Number(key), state.strings);
      return slot === null ? '' : slot.name;
    }

    function showSelection(mode, key) {
      clearSelectionMarks();
      var chipSelector = mode === 'tier' ? '.lt-seed-tchip' : '.lt-seed-chip';
      var chips = rootEl.querySelectorAll(chipSelector);
      var i;
      for (i = 0; i < chips.length; i++) chips[i].classList.add('is-tgt');
      var own = buttonFor(mode, key);
      if (own !== null) {
        own.setAttribute('aria-pressed', 'true');
        own.parentNode.classList.remove('is-tgt');
        own.parentNode.classList.add('is-sel');
      }
      if (mode === 'tier') {
        var tiers = rootEl.querySelectorAll('.lt-seed-tier');
        for (i = 0; i < tiers.length; i++) {
          var end = el(
            'button',
            'lt-seed-tend',
            str('tier_end', { letter: tiers[i].getAttribute('data-letter') })
          );
          end.type = 'button';
          end.setAttribute('data-tier-end', tiers[i].getAttribute('data-tier'));
          tiers[i].appendChild(end);
        }
      }
      var hint = el('div', 'lt-seed-hintbar');
      hint.setAttribute('role', 'status');
      var text = el('span');
      var parts = str('hint').split('%(name)s');
      text.appendChild(document.createTextNode(parts[0]));
      text.appendChild(el('b', null, nameOf(mode, key)));
      text.appendChild(document.createTextNode(parts.slice(1).join('')));
      hint.appendChild(text);
      hint.appendChild(actionButton(str('cancel'), 'data-cancel'));
      var panels = rootEl.querySelector('.lt-seed-panels');
      if (panels !== null) {
        panels.parentNode.insertBefore(hint, panels);
      } else {
        rootEl.appendChild(hint);
      }
    }

    function cancelSelection() {
      var mode = state.selectionMode;
      var result = nextSelection(state.selection, { type: 'cancel' });
      state.selection = { selected: result.selected };
      state.selectionMode = null;
      if (result.effect.type === 'cancelled') {
        clearSelectionMarks();
        announce(str('cancelled'));
        var button = buttonFor(mode, result.effect.key);
        if (button !== null) button.focus();
      }
    }

    function pick(mode, key) {
      if (state.busy) return;
      if (refuseWhileStale()) return;
      if (isLocked()) {
        announce(str('locked', { reason: state.board.locked_reason || '' }));
        return;
      }
      var event = { type: 'pick', key: key };
      var result = nextSelection(state.selection, event);
      var previousMode = state.selectionMode;
      state.selection = { selected: result.selected };
      var effect = result.effect;
      if (effect.type === 'selected') {
        state.selectionMode = mode;
        showSelection(mode, key);
        announce(str('selected', { name: nameOf(mode, key) }));
        var own = buttonFor(mode, key);
        if (own !== null) own.focus();
      } else if (effect.type === 'cancelled') {
        state.selectionMode = null;
        clearSelectionMarks();
        announce(str('cancelled'));
        var again = buttonFor(previousMode, key);
        if (again !== null) again.focus();
      } else if (effect.type === 'apply') {
        state.selectionMode = null;
        clearSelectionMarks();
        if (mode === 'tier') {
          var target = findContestant(state.board, key);
          if (target !== null) {
            perform(moveFields(effect.from, target.tier.index, key), {
              focus: { mode: 'tier', key: effect.from },
              move: effect.from
            });
          }
        } else {
          perform(
            { action: 'swap', p: String(effect.from), q: String(effect.to) },
            {
              focus: { mode: 'slot', key: effect.to },
              swap: [Number(effect.from), Number(effect.to)]
            }
          );
        }
      }
    }

    // -------------------------------------------------------------- //
    // requests

    // A saved change the page could not show leaves the old layout on
    // screen, so a slot index would name another contestant. The board
    // takes no action until the page is reloaded.
    function refuseWhileStale() {
      if (!state.stale) return false;
      announce(str('saved_reload'));
      return true;
    }

    function perform(fields, options) {
      options = options || {};
      if (state.busy) {
        if (dropNeedsRevert(options, 'busy')) {
          // Wait for the running request, so the reload sees its result.
          (state.inflight || Promise.resolve()).then(function () {
            refresh(function () {});
          });
        }
        return;
      }
      if (refuseWhileStale()) return;
      state.busy = true;
      state.last = { fields: fields, options: options };
      setSave('busy');
      var body = {
        target: state.board.target,
        version: String(state.board.version)
      };
      Object.keys(fields).forEach(function (key) {
        body[key] = fields[key];
      });
      var before = state.board;
      state.inflight = fetch(rootEl.getAttribute('data-action-url'), {
        method: 'POST',
        credentials: 'same-origin',
        headers: {
          Accept: 'application/json',
          'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8'
        },
        body: encode(body)
      })
        .then(function (response) {
          return response.json().then(
            function (data) {
              return { status: response.status, data: data };
            },
            function () {
              return { status: response.status, data: {} };
            }
          );
        })
        .then(function (result) {
          if (result.status === 200 && result.data.board) {
            handleSuccess(before, result.data.board, options);
          } else if (result.status === 409) {
            if (dropNeedsRevert(options, 'conflict')) {
              refresh(function () {
                state.busy = false;
                showConflict(result.data.error || '');
              });
              return;
            }
            state.busy = false;
            showConflict(result.data.error || '');
          } else {
            handleRefusal(result.data.error, options);
          }
        })
        .catch(function () {
          state.busy = false;
          setSave('err');
          announce(str('failed'));
        });
    }

    function handleSuccess(before, after, options) {
      var message = '';
      if (options.swap) {
        message = describeSwap(
          before,
          options.swap[0],
          options.swap[1],
          state.strings
        );
      } else if (options.move) {
        message = describeMove(
          after,
          options.move,
          state.strings,
          before.fix_count > 0
        );
      } else if (options.replay) {
        message = describeReplay(after, state.strings);
      } else if (options.separate) {
        message = describeSeparation(before, after, state.strings);
      }
      // `state.board` changes only with the page: attach() reads it there.
      refresh(function () {
        state.busy = false;
        setSave('ok');
        announce(message);
        if (options.replay) {
          showReplayMessage(message, 'is-ok');
        }
        if (options.tierNotice) {
          showTierNotice(before, after, options.move);
        }
        if (options.reseedNotice) {
          showReseedNotice(before);
        }
        if (options.separate) {
          showBanner(banner('ok', message, str('notice_separated_clean')));
        }
        restoreFocus(options.focus);
      }, function () {
        state.busy = false;
        state.stale = true;
        destroySortables();
        setSave('ok');
        announce(str('saved_reload'));
        showBanner(
          banner('info', str('saved_reload'), '', [
            actionButton(str('reload'), 'data-reload')
          ])
        );
      });
    }

    function handleRefusal(error, options) {
      var message = error || str('failed');
      if (options.replay) {
        state.busy = false;
        setSave('ok');
        showReplayMessage(message, 'is-err');
        announce(message);
        return;
      }
      refresh(function () {
        state.busy = false;
        showBanner(banner('err', message));
        setSave('err');
        announce(message);
      });
    }

    function restoreFocus(target) {
      if (!target) return;
      var button = buttonFor(target.mode, target.key);
      if (button !== null) button.focus();
    }

    function showReplayMessage(text, kind) {
      var node = rootEl.querySelector('[data-replay-message]');
      var input = rootEl.querySelector('#seeding-replay-code');
      if (node === null) return;
      node.className = 'lt-seed-msg ' + kind;
      node.setAttribute('role', kind === 'is-err' ? 'alert' : 'status');
      node.textContent = text;
      node.hidden = false;
      if (input !== null) {
        if (kind === 'is-err') {
          input.classList.add('is-invalid');
          input.setAttribute('aria-invalid', 'true');
        } else {
          input.value = '';
        }
        input.focus();
      }
    }

    function showTierNotice(before, after, contestantId) {
      var open = actionButton(str('open_layout'), 'data-open-layout');
      var body = describeTierMove(before, after, contestantId, state.strings);
      showBanner(banner('info', str(tierNoticeKind(before)), body, [open]));
    }

    function showReseedNotice(before) {
      showBanner(
        banner(
          'ok',
          str('notice_reseeded', { names: before.stale_leavers.join(', ') })
        )
      );
    }

    function refresh(done, onFail) {
      fetch(rootEl.getAttribute('data-page-url'), {
        credentials: 'same-origin',
        headers: { Accept: 'text/html' }
      })
        .then(function (response) {
          return response.text().then(function (text) {
            return { response: response, text: text };
          });
        })
        .then(function (result) {
          var doc = new DOMParser().parseFromString(result.text, 'text/html');
          var fresh = doc.querySelector('[data-lt-seed-root]');
          if (fresh === null) {
            window.location.reload();
            return;
          }
          rootEl.innerHTML = fresh.innerHTML;
          attach();
          done();
        })
        .catch(function () {
          if (onFail) {
            onFail();
            return;
          }
          state.busy = false;
          setSave('err');
          announce(str('failed'));
        });
    }

    // -------------------------------------------------------------- //
    // tabs

    function showTab(name) {
      var tabs = rootEl.querySelectorAll('.lt-seed-tab');
      var panels = rootEl.querySelectorAll('.lt-seed-boardp');
      var i;
      state.tab = name;
      for (i = 0; i < tabs.length; i++) {
        var on = tabs[i].getAttribute('data-tab') === name;
        tabs[i].classList.toggle('is-on', on);
        tabs[i].setAttribute('aria-selected', on ? 'true' : 'false');
      }
      for (i = 0; i < panels.length; i++) {
        panels[i].hidden = panels[i].getAttribute('data-panel') !== name;
      }
    }

    function defaultTab() {
      return state.board.format === 'FFA' ? 'tier' : 'layout';
    }

    // -------------------------------------------------------------- //
    // drag and drop

    function destroySortables() {
      state.sortables.forEach(function (sortable) {
        sortable.destroy();
      });
      state.sortables = [];
    }

    function setupSortables() {
      destroySortables();
      if (typeof Sortable === 'undefined' || isLocked()) return;
      var lists = rootEl.querySelectorAll('[data-swap-list]');
      var dragged = null;
      var related = null;
      var i;
      for (i = 0; i < lists.length; i++) {
        state.sortables.push(
          Sortable.create(lists[i], {
            group: 'lt-seed-slots',
            draggable: '.lt-seed-chip',
            handle: '.lt-seed-hd',
            swap: true,
            swapClass: 'is-over',
            animation: 150,
            onStart: function (evt) {
              dragged = evt.item.getAttribute('data-i');
              related = null;
            },
            onMove: function (evt) {
              if (evt.related && evt.related.getAttribute('data-i') !== null) {
                related = evt.related.getAttribute('data-i');
              }
              return true;
            },
            onEnd: function () {
              var p = dragged;
              var q = related;
              dragged = null;
              related = null;
              if (p === null || q === null || p === q) return;
              cancelSelection();
              perform(
                { action: 'swap', p: p, q: q },
                {
                  focus: { mode: 'slot', key: q },
                  swap: [Number(p), Number(q)],
                  drag: true
                }
              );
            }
          })
        );
      }
      var tierLists = rootEl.querySelectorAll('.lt-seed-tlist');
      for (i = 0; i < tierLists.length; i++) {
        state.sortables.push(
          Sortable.create(tierLists[i], {
            group: 'lt-seed-tiers',
            draggable: '.lt-seed-tchip',
            handle: '.lt-seed-hd',
            animation: 150,
            onEnd: function (evt) {
              var id = evt.item.getAttribute('data-id');
              var next = evt.item.nextElementSibling;
              var tier = evt.to.parentNode.getAttribute('data-tier');
              if (evt.from === evt.to && evt.oldIndex === evt.newIndex) return;
              cancelSelection();
              perform(
                moveFields(id, tier, next ? next.getAttribute('data-id') : null),
                {
                  focus: { mode: 'tier', key: id },
                  move: id,
                  tierNotice: true,
                  drag: true
                }
              );
            }
          })
        );
      }
    }

    // -------------------------------------------------------------- //
    // confirm dialog

    function confirmDialog(title, body, okLabel, onOk) {
      openDialog({
        title: title,
        body: body,
        okLabel: okLabel,
        cancelLabel: str('cancel'),
        onOk: onOk
      });
    }

    // -------------------------------------------------------------- //
    // events

    function fieldsFromForm(form, submitter) {
      var data = submitter ? new FormData(form, submitter) : new FormData(form);
      var fields = {};
      data.forEach(function (value, key) {
        if (key !== 'target' && key !== 'version') fields[key] = value;
      });
      if (submitter && submitter.name) fields[submitter.name] = submitter.value;
      return fields;
    }

    rootEl.addEventListener('click', function (event) {
      var target = event.target;
      var node = target.closest ? target.closest('button') : null;
      if (node === null || !rootEl.contains(node)) return;

      if (node.hasAttribute('data-retry')) {
        if (state.last) {
          var last = state.last;
          perform(last.fields, last.options);
        }
        return;
      }
      if (node.hasAttribute('data-reload')) {
        window.location.reload();
        return;
      }
      if (node.hasAttribute('data-cancel')) {
        cancelSelection();
        return;
      }
      if (node.hasAttribute('data-open-layout')) {
        showTab('layout');
        return;
      }
      if (node.hasAttribute('data-copy')) {
        var source = rootEl.querySelector(node.getAttribute('data-copy'));
        if (source !== null && navigator.clipboard) {
          navigator.clipboard.writeText(source.textContent.trim()).then(
            function () {
              announce(str('copied'));
            },
            function () {}
          );
        }
        return;
      }
      if (node.classList.contains('lt-seed-tab')) {
        cancelSelection();
        showTab(node.getAttribute('data-tab'));
        return;
      }
      if (node.hasAttribute('data-tier-end')) {
        if (state.selectionMode === 'tier' && state.selection.selected) {
          var id = state.selection.selected;
          var tier = node.getAttribute('data-tier-end');
          state.selection = { selected: null };
          state.selectionMode = null;
          clearSelectionMarks();
          perform(moveFields(id, tier, null), {
            focus: { mode: 'tier', key: id },
            move: id,
            tierNotice: true
          });
        }
        return;
      }
      if (node.hasAttribute('data-move')) {
        var holder = node.closest('.lt-seed-tchip');
        var step = stepTarget(
          state.board,
          holder.getAttribute('data-id'),
          node.getAttribute('data-move')
        );
        if (step !== null && !state.busy) {
          var moved = holder.getAttribute('data-id');
          perform(moveFields(moved, step.tier, step.ref_id), {
            focus: { mode: 'tier', key: moved },
            move: moved,
            tierNotice: true
          });
        }
        return;
      }
      if (node.classList.contains('lt-seed-cb')) {
        var chip = node.closest('.lt-seed-chip');
        var tierChip = node.closest('.lt-seed-tchip');
        if (chip !== null) {
          pick('slot', chip.getAttribute('data-i'));
        } else if (tierChip !== null) {
          pick('tier', tierChip.getAttribute('data-id'));
        }
      }
    });

    document.addEventListener('keydown', function (event) {
      if (
        event.key === 'Escape' &&
        !event.defaultPrevented &&
        state.selection.selected !== null
      ) {
        cancelSelection();
      }
    });

    rootEl.addEventListener('submit', function (event) {
      var form = event.target;
      var actionUrl = rootEl.getAttribute('data-action-url');
      var isAction = form.getAttribute('action') === actionUrl;
      var plain = !isAction && form.getAttribute('data-confirm-title') === null;
      if (!plain) {
        event.preventDefault();
        if (state.busy || refuseWhileStale()) return;
      }
      var submitter = event.submitter || null;
      var native = routeSubmit(form, isAction, {
        confirm: function (title, onOk) {
          confirmDialog(
            title,
            form.getAttribute('data-confirm-body'),
            form.getAttribute('data-confirm-ok'),
            onOk
          );
        },
        post: function () {
          form.submit();
        },
        perform: function () {
          var fields = fieldsFromForm(form, submitter);
          var options = {};
          if (form.hasAttribute('data-replay')) {
            options.replay = true;
          } else if (fields.action === 'set_tier_count') {
            options.tierNotice = true;
          } else if (fields.action === 'reseed_keep_tiers') {
            options.reseedNotice = true;
          } else if (fields.action === 'separate') {
            options.separate = true;
          }
          perform(fields, options);
        }
      });
      if (plain && !native) event.preventDefault();
    });

    // A page restored from the back-forward cache keeps its sent marks.
    window.addEventListener('pageshow', function (event) {
      if (!event.persisted) return;
      var sent = rootEl.querySelectorAll('[data-lt-sent]');
      for (var i = 0; i < sent.length; i++) sent[i].removeAttribute('data-lt-sent');
    });

    // -------------------------------------------------------------- //

    // Forms outside the board (the release) carry the draft's version.
    function syncVersionInputs(board) {
      var inputs = document.querySelectorAll('input[data-lt-playoff-version]');
      for (var i = 0; i < inputs.length; i++) {
        if (board.target === 'playoff') inputs[i].value = String(board.version);
      }
    }

    function attach() {
      state.board = readBoard(rootEl);
      if (state.board === null) return;
      state.strings = state.board.strings || {};
      syncVersionInputs(state.board);
      state.selection = { selected: null };
      state.selectionMode = null;
      var details = rootEl.querySelector('.lt-seed-nojs');
      if (details !== null) details.removeAttribute('open');
      var copy = rootEl.querySelectorAll('[data-copy]');
      for (var i = 0; i < copy.length; i++) copy[i].hidden = false;
      var known = rootEl.querySelector(
        '.lt-seed-tab[data-tab="' + state.tab + '"]'
      );
      showTab(known !== null ? state.tab : defaultTab());
      setupSortables();
    }

    attach();
  }

  // ------------------------------------------------------------------ //
  // qualification page

  function readStrings(rootEl) {
    var script = rootEl.querySelector('script[data-lt-strings]');
    if (script === null) return {};
    try {
      return JSON.parse(script.textContent);
    } catch (e) {
      return {};
    }
  }

  function initOrderingBoard(rootEl) {
    if (rootEl.getAttribute('data-lt-order-ready')) return;
    rootEl.setAttribute('data-lt-order-ready', '1');
    rootEl.classList.add('is-js');

    var strings = readStrings(rootEl);
    var live = el('div', 'lt-seed-sr');
    live.setAttribute('aria-live', 'polite');
    rootEl.parentNode.insertBefore(live, rootEl.nextSibling);

    function announce(text) {
      if (!text) return;
      live.textContent = '';
      setTimeout(function () {
        live.textContent = text;
      }, 30);
    }

    function byId(id) {
      return document.getElementById(id);
    }

    // -------------------------------------------------------------- //
    // tie ordering

    function setupOrder(form) {
      var fallback = form.querySelector('[data-lt-order-fallback]');
      var list = form.querySelector('[data-lt-order-list]');
      if (list === null) return;
      if (fallback !== null) {
        fallback.hidden = true;
        var selects = fallback.querySelectorAll('select');
        for (var i = 0; i < selects.length; i++) selects[i].disabled = true;
      }
      list.hidden = false;
      var values = list.querySelectorAll('[data-order-value]');
      for (var j = 0; j < values.length; j++) values[j].name = 'order';
      var firstPlace = Number(list.getAttribute('data-first-place')) || 1;

      function items() {
        return Array.prototype.slice.call(list.querySelectorAll('.lt-seed-obi'));
      }

      function sync() {
        var rows = items();
        rows.forEach(function (row, index) {
          row.querySelector('[data-order-place]').textContent = fill(
            strings.place || '',
            { n: firstPlace + index }
          );
          row.querySelector('[data-order-move="up"]').disabled = index === 0;
          row.querySelector('[data-order-move="down"]').disabled =
            index === rows.length - 1;
        });
      }

      function nameOf(row) {
        return row.getAttribute('data-name');
      }

      list.addEventListener('click', function (event) {
        var button = event.target.closest
          ? event.target.closest('[data-order-move]')
          : null;
        if (button === null || !list.contains(button)) return;
        var rows = items();
        var row = button.closest('.lt-seed-obi');
        var index = rows.indexOf(row);
        var direction = button.getAttribute('data-order-move');
        var result = moveInOrder(
          rows.map(function (r) {
            return r.getAttribute('data-id');
          }),
          index,
          direction
        );
        if (!result.moved) return;
        if (direction === 'up') {
          list.insertBefore(row, rows[result.index]);
        } else {
          list.insertBefore(row, rows[result.index].nextSibling);
        }
        sync();
        announce(describeOrderMove(nameOf(row), firstPlace, result.index, strings));
        var same = row.querySelector('[data-order-move="' + direction + '"]');
        var other = row.querySelector(
          '[data-order-move="' + (direction === 'up' ? 'down' : 'up') + '"]'
        );
        (same.disabled ? other : same).focus();
      });

      if (typeof Sortable !== 'undefined') {
        Sortable.create(list, {
          draggable: '.lt-seed-obi',
          handle: '.lt-seed-hd',
          animation: 150,
          onEnd: function (evt) {
            if (evt.oldIndex === evt.newIndex) return;
            sync();
            announce(
              describeOrderMove(
                nameOf(evt.item),
                firstPlace,
                items().indexOf(evt.item),
                strings
              )
            );
          }
        });
      }
      sync();
    }

    // -------------------------------------------------------------- //
    // reasons, reveal and confirm

    function setupReason(form) {
      var field = form.querySelector('[data-lt-reason]');
      var submit = form.querySelector('[data-lt-reason-submit]');
      var why = form.querySelector('[data-lt-reason-why]');
      if (field === null || submit === null) return;
      function sync() {
        var filled = reasonFilled(field.value);
        submit.disabled = !filled;
        if (why !== null) {
          why.hidden = filled;
          if (filled) submit.removeAttribute('aria-describedby');
          else if (why.id) submit.setAttribute('aria-describedby', why.id);
        }
      }
      field.addEventListener('input', sync);
      sync();
    }

    function setupReveal(button) {
      var target = byId(button.getAttribute('data-lt-reveal'));
      if (target === null) return;
      button.hidden = false;
      target.hidden = true;
      button.addEventListener('click', function () {
        target.hidden = false;
        button.hidden = true;
        var field = target.querySelector('textarea');
        if (field !== null) field.focus();
      });
    }

    function setupHide(button) {
      var target = byId(button.getAttribute('data-lt-hide'));
      if (target === null) return;
      var opener = rootEl.querySelector(
        '[data-lt-reveal="' + button.getAttribute('data-lt-hide') + '"]'
      );
      button.hidden = false;
      button.addEventListener('click', function () {
        target.hidden = true;
        if (opener !== null) {
          opener.hidden = false;
          opener.focus();
        }
      });
    }

    var forms = rootEl.querySelectorAll('[data-lt-order]');
    var i;
    for (i = 0; i < forms.length; i++) setupOrder(forms[i]);
    var gated = rootEl.querySelectorAll('[data-lt-reason-form]');
    for (i = 0; i < gated.length; i++) setupReason(gated[i]);
    var openers = rootEl.querySelectorAll('[data-lt-reveal]');
    for (i = 0; i < openers.length; i++) setupReveal(openers[i]);
    var closers = rootEl.querySelectorAll('[data-lt-hide]');
    for (i = 0; i < closers.length; i++) setupHide(closers[i]);

    rootEl.addEventListener('submit', function (event) {
      var form = event.target;
      var title = form.getAttribute('data-confirm-title');
      if (title === null) return;
      if (form.closest('[data-lt-seed-root]') !== null) return;
      event.preventDefault();
      openDialog({
        title: title,
        body: form.getAttribute('data-confirm-body'),
        okLabel: form.getAttribute('data-confirm-ok'),
        cancelLabel: strings.cancel || '',
        danger: form.hasAttribute('data-confirm-danger'),
        onOk: function () {
          form.submit();
        }
      });
    });
  }

  function init(doc) {
    var roots = doc.querySelectorAll('[data-lt-seed-root]');
    var i;
    for (i = 0; i < roots.length; i++) initBoard(roots[i]);
    var orderRoots = doc.querySelectorAll('[data-lt-order-root]');
    for (i = 0; i < orderRoots.length; i++) initOrderingBoard(orderRoots[i]);
  }

  return {
    init: init,
    initBoard: initBoard,
    initOrderingBoard: initOrderingBoard,
    fill: fill,
    nextSelection: nextSelection,
    describeSwap: describeSwap,
    describeMove: describeMove,
    describeReplay: describeReplay,
    separationPairs: separationPairs,
    describeSeparation: describeSeparation,
    moveFields: moveFields,
    stepTarget: stepTarget,
    tierNoticeKind: tierNoticeKind,
    describeTierMove: describeTierMove,
    dropNeedsRevert: dropNeedsRevert,
    firstSubmit: firstSubmit,
    routeSubmit: routeSubmit,
    moveInOrder: moveInOrder,
    describeOrderMove: describeOrderMove,
    reasonFilled: reasonFilled,
    dialogLines: dialogLines
  };
});
