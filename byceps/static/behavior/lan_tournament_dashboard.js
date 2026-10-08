/*
 * Orga dashboard: polling, freshness, the check form and the pin.
 *
 * The page is complete without this script: every control is a native link
 * or form and the refresh is a plain GET. The script only enhances it. It
 * polls the server for the rendered panel, replaces the panel's content and
 * keeps what the viewer is doing (open forms, the disclosure state, the
 * focus). It never computes a tier, a colour, a due time or a duration,
 * never raises a revision, never answers for the server and never posts on
 * its own: a check or a pin is sent only by the viewer's own submit, and it
 * counts as saved only after the server says so. All texts come from the
 * root's `data-labels`, rendered on the server.
 *
 * Node: `module.exports` (`initDashboard`, the pure helpers). Browser: the
 * global `LtDashboard`.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) {
    module.exports = factory();
  } else {
    root.LtDashboard = factory();
    if (typeof document !== 'undefined') {
      var start = function () {
        root.LtDashboard.init(document);
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

  var ROOT = '[data-lt-dashboard-root]';
  var COMMENT_MAX = 500;
  var TIMEOUT_MS = 15000;
  var AGE_TICK_MS = 60000;
  var MIN_POLL_SECONDS = 1;
  var JS_MARK = 'data-ltd-js';
  var REFUSALS = { unavailable: 1, stale: 1, refused: 1, invalid: 1 };

  // What the script reads from the server's markup. The unit tests check
  // every entry against the real panel.
  var HOOKS = {
    top: '.ltd-top',
    scope: '.ltd-top .ltd-scope',
    fresh: '.ltd-fresh',
    freshTime: '.ltd-fresh b',
    freshZone: '.ltd-fresh .ltd-tz',
    status: '.ltd-fst',
    statusAuto: '.ltd-fst .ltd-fst-auto',
    refreshForm: 'form.ltd-refresh',
    refreshButton: 'form.ltd-refresh button[data-refresh]',
    summary: '.ltd-sum',
    tile: '.ltd-sum li.ltd-st',
    tileLink: '.ltd-sum li.ltd-st a[href]',
    filters: 'form.ltd-filters',
    countBlock: '.ltd-count',
    countHeading: '#ltd-count',
    rows: 'ol.ltd-rows',
    row: 'li.ltd-row',
    article: 'li.ltd-row article',
    rowHeading: 'li.ltd-row h3',
    rowContext: '.ltd-ctx a',
    rowLocation: '.ltd-loc',
    wait: '.ltd-times .is-key dd',
    act: '.ltd-act',
    opener: 'button[data-ack-open]',
    cancel: 'button[data-ack-cancel]',
    ackBand: '.ltd-ack',
    ackForm: 'form[data-ack]',
    comment: 'form[data-ack] textarea',
    counter: 'form[data-ack] .ltd-cnt',
    buttonRow: 'form[data-ack] .ltd-frow',
    record: '.ltd-rec',
    history: 'button[data-hist]',
    pinForm: 'form.ltd-pin',
    pinButton: 'form.ltd-pin button[data-pin]',
    pinSignal: '.ltd-sig.s-pin',
    live: '.ltd-sr[data-live]'
  };

  // ------------------------------------------------------------------ //
  // pure helpers

  // Fill the `%(name)s` and `%(name)d` placeholders of a server message.
  function fill(template, params) {
    return String(template).replace(
      /%(?:\((\w+)\)[sd]|%)/g,
      function (whole, key) {
        if (whole === '%%') return '%';
        return Object.prototype.hasOwnProperty.call(params || {}, key)
          ? String(params[key])
          : whole;
      }
    );
  }

  function parseInstant(value) {
    if (typeof value !== 'string') return null;
    var ms = Date.parse(value);
    return isNaN(ms) ? null : ms;
  }

  // Count code points, like the server counts the comment.
  function codePoints(text) {
    var n = 0;
    for (var i = 0; i < text.length; i++) {
      var c = text.charCodeAt(i);
      if (c >= 0xd800 && c <= 0xdbff && i + 1 < text.length) {
        var d = text.charCodeAt(i + 1);
        if (d >= 0xdc00 && d <= 0xdfff) i++;
      }
      n++;
    }
    return n;
  }

  // The path of a URL; `base` makes a relative one absolute.
  function pathOf(url, base) {
    try {
      return new URL(url, base).pathname;
    } catch (e) {
      return null;
    }
  }

  // The tournament overview that belongs to a dashboard's poll URL: the
  // party overview of the admin, the index of the site. The unit tests pin
  // this against the real routes.
  function overviewUrl(surface, pollUrl, base) {
    var path = pathOf(pollUrl, base);
    if (path === null) return null;
    var parts = path.split('/');
    parts.splice(-2, 2);
    var joined = parts.join('/');
    return surface === 'site' ? joined + '/' : joined;
  }

  // Sort a response into what the page does about it. The order is the
  // contract: an outage is never access loss, and a failed form check is
  // never lost authority.
  function classifyResponse(result, loginPath) {
    if (result.failure) {
      return { kind: 'transient', reason: result.failure };
    }
    var status = result.status;
    if (status >= 500) return { kind: 'transient', reason: 'server' };
    if (
      result.redirected &&
      loginPath &&
      pathOf(result.url) === loginPath
    ) {
      return { kind: 'session', reason: 'redirect' };
    }
    var data = result.json || null;
    var code = data && typeof data.error === 'string' ? data.error : null;
    if (status === 401 || code === 'session_expired') {
      return { kind: 'session', reason: 'expired' };
    }
    if (code === 'csrf_invalid') return { kind: 'csrf', data: data };
    if (status === 403 || code === 'access_revoked') {
      return { kind: 'access', reason: 'revoked' };
    }
    if (status >= 200 && status < 300) {
      return data
        ? { kind: 'ok', data: data }
        : { kind: 'transient', reason: 'format' };
    }
    if (status >= 400 && status < 500 && REFUSALS[code] === 1) {
      return { kind: 'refusal', code: code, data: data };
    }
    return { kind: 'transient', reason: 'client' };
  }

  function readFragment(source) {
    if (!source || typeof source.html !== 'string') return null;
    var asOf = parseInstant(source.as_of);
    if (asOf === null) return null;
    return {
      html: source.html,
      asOf: asOf,
      pollSeconds: Number(source.poll_seconds)
    };
  }

  // ------------------------------------------------------------------ //
  // dom helpers

  function all(scope, selector) {
    return Array.prototype.slice.call(scope.querySelectorAll(selector));
  }

  // The three writers below leave a node alone when it already is as asked:
  // an attribute set to its own value is still a change to a reader.
  function setHidden(el, hidden) {
    setFlag(el, 'hidden', hidden);
  }

  function setFlag(el, name, on) {
    if (el.hasAttribute(name) === on) return;
    if (on) el.setAttribute(name, '');
    else el.removeAttribute(name);
  }

  function toggleClass(el, name, on) {
    if (el.classList.contains(name) === on) return;
    if (on) el.classList.add(name);
    else el.classList.remove(name);
  }

  // The text a reader gets: what sits under an `aria-hidden` icon is left out.
  function visibleText(el) {
    var out = '';
    Array.prototype.forEach.call(el.childNodes, function (node) {
      if (node.nodeType === 3) out += node.nodeValue;
      else if (node.nodeType === 1 && node.getAttribute('aria-hidden') !== 'true') {
        out += visibleText(node);
      }
    });
    return out;
  }

  function removeNode(node) {
    if (node && node.parentNode) node.parentNode.removeChild(node);
  }

  function tierLetter(el) {
    var found = null;
    ['r', 'y', 'g'].forEach(function (letter) {
      if (el && el.classList.contains('t-' + letter)) found = letter;
    });
    return found;
  }

  // ------------------------------------------------------------------ //
  // comparing and arranging the server's markup

  // The element itself matches, not only an ancestor of it.
  function isA(el, selector) {
    return el.closest(selector) === el;
  }

  function always() {
    return true;
  }

  // A string that is equal for two nodes exactly when their markup is
  // equal. An element for which `skip` answers true counts with its tag
  // and attributes only.
  function signature(node, skip) {
    if (node.nodeType === 3) return JSON.stringify(node.nodeValue);
    if (node.nodeType !== 1) return '';
    var attrs = Array.prototype.map
      .call(node.attributes, function (attr) {
        return [attr.name, attr.value];
      })
      .sort(function (a, b) {
        return a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0;
      });
    var head = node.tagName + JSON.stringify(attrs);
    if (skip && skip(node)) return head;
    return (
      head +
      '[' +
      Array.prototype.map
        .call(node.childNodes, function (child) {
          return signature(child, skip);
        })
        .join(',') +
      ']'
    );
  }

  // The tag, the attributes and the loose text of an element, without its
  // element children.
  function shellOf(el) {
    var text = [];
    Array.prototype.forEach.call(el.childNodes, function (child) {
      if (child.nodeType === 3) text.push(child.nodeValue);
    });
    return signature(el, always) + JSON.stringify(text);
  }

  function unitKey(node) {
    var first = (node.getAttribute('class') || '').split(/\s+/)[0];
    return node.tagName.toLowerCase() + (first ? '.' + first : '');
  }

  function idKey(node) {
    return node.getAttribute('id') || '';
  }

  // The element children of `parent` with a key each; a repeated key gets
  // its number.
  function keyedChildren(parent, keyOf) {
    var seen = Object.create(null);
    var out = [];
    Array.prototype.forEach.call(parent.childNodes, function (node) {
      if (node.nodeType !== 1) return;
      var base = keyOf(node);
      seen[base] = (seen[base] || 0) + 1;
      out.push({
        key: seen[base] > 1 ? base + '#' + seen[base] : base,
        node: node
      });
    });
    return out;
  }

  // Which of the wanted nodes can stay where they stand: the longest run
  // whose current order already agrees with the wanted one. `positions`
  // holds the current index of each wanted node, or -1 for a new one.
  function stayers(positions) {
    var length = [];
    var previous = [];
    var best = -1;
    positions.forEach(function (at, i) {
      length[i] = 0;
      previous[i] = -1;
      if (at < 0) return;
      length[i] = 1;
      for (var j = 0; j < i; j++) {
        if (positions[j] >= 0 && positions[j] < at && length[j] + 1 > length[i]) {
          length[i] = length[j] + 1;
          previous[i] = j;
        }
      }
      if (best < 0 || length[i] > length[best]) best = i;
    });
    var stay = positions.map(function () {
      return false;
    });
    for (var k = best; k >= 0; k = previous[k]) stay[k] = true;
    return stay;
  }

  // Make the element children of `parent` exactly `wanted`, in that order,
  // and move as few nodes as possible: a node that stands in its place is
  // not touched, so it keeps its focus.
  function arrange(parent, wanted) {
    Array.prototype.slice.call(parent.childNodes).forEach(function (node) {
      if (node.nodeType === 1 && wanted.indexOf(node) < 0) {
        parent.removeChild(node);
      }
    });
    var current = Array.prototype.filter.call(parent.childNodes, function (node) {
      return node.nodeType === 1;
    });
    var stay = stayers(
      wanted.map(function (node) {
        return current.indexOf(node);
      })
    );
    var next = null;
    for (var i = wanted.length - 1; i >= 0; i--) {
      if (!stay[i]) parent.insertBefore(wanted[i], next);
      next = wanted[i];
    }
  }

  var controllers = typeof WeakMap === 'function' ? new WeakMap() : null;

  // ------------------------------------------------------------------ //
  // the controller

  function initDashboard(rootEl, options) {
    if (rootEl === null || rootEl === undefined) return null;
    if (controllers !== null && controllers.has(rootEl)) {
      return controllers.get(rootEl);
    }
    options = options || {};
    var doc = options.document || rootEl.ownerDocument;
    var win = options.window || (typeof window !== 'undefined' ? window : {});
    var fetchFn = Object.prototype.hasOwnProperty.call(options, 'fetch')
      ? options.fetch
      : typeof fetch === 'function'
        ? fetch
        : null;
    // Without these the native markup stays as it is and keeps working.
    if (typeof fetchFn !== 'function' || !doc || !rootEl.closest) return null;
    var AC =
      options.AbortController ||
      (typeof AbortController === 'function' ? AbortController : null);
    var setT =
      options.setTimeout ||
      function (fn, ms) {
        return setTimeout(fn, ms);
      };
    var clearT =
      options.clearTimeout ||
      function (handle) {
        clearTimeout(handle);
      };
    var now = options.now || Date.now;
    var isHidden =
      options.isHidden ||
      function () {
        return !!doc.hidden;
      };
    var loc = options.location || win.location;
    var hist = options.history || win.history;
    var timeoutMs = options.timeoutMs || TIMEOUT_MS;
    var parseFragment =
      options.parseFragment ||
      function (html) {
        if (typeof DOMParser !== 'function') return null;
        var parsed = new DOMParser().parseFromString(html, 'text/html');
        return parsed.querySelector(ROOT);
      };

    var labels = readLabels(rootEl);
    var st = {
      pollSeconds: pollSecondsOf(rootEl.getAttribute('data-poll-seconds')),
      shownAsOf: parseInstant(rootEl.getAttribute('data-as-of')) || 0,
      receivedAt: now(),
      stopped: false,
      failed: false,
      loading: false,
      submitting: 0,
      confirming: false,
      pendingResume: false,
      navigated: false,
      timer: null,
      ageTimer: null,
      active: null,
      sequence: 0,
      shown: null
    };
    var pending = typeof WeakMap === 'function' ? new WeakMap() : null;
    // What the viewer chose in the filters or the scope but has not applied.
    var dirty = {};

    // -------------------------------------------------------------- //
    // small readers

    function readLabels(el) {
      try {
        var parsed = JSON.parse(el.getAttribute('data-labels') || '{}');
        return parsed && typeof parsed === 'object' ? parsed : {};
      } catch (e) {
        return {};
      }
    }

    function pollSecondsOf(raw) {
      var seconds = Number(raw);
      return seconds >= MIN_POLL_SECONDS ? seconds : 30;
    }

    function label(key, params) {
      var text = labels[key];
      if (typeof text !== 'string') return '';
      return params ? fill(text, params) : text;
    }

    function make(tag, attrs, text) {
      var el = doc.createElement(tag);
      Object.keys(attrs || {}).forEach(function (name) {
        el.setAttribute(name, attrs[name]);
      });
      if (text !== undefined) el.textContent = text;
      return el;
    }

    function byId(id) {
      return id ? doc.getElementById(id) : null;
    }

    function pollUrl() {
      return rootEl.getAttribute('data-poll-url') || '';
    }

    function baseHref() {
      return (loc && loc.href) || undefined;
    }

    // A URL on this page's own origin, or null.
    function sameOrigin(href) {
      if (!href) return null;
      try {
        var base = new URL(baseHref());
        var url = new URL(href, base);
        return url.origin === base.origin ? url : null;
      } catch (e) {
        return null;
      }
    }

    function loginPath() {
      return pathOf(rootEl.getAttribute('data-login-url') || '', baseHref());
    }

    function hiddenNow() {
      try {
        return !!isHidden();
      } catch (e) {
        return false;
      }
    }

    function openForms() {
      return all(rootEl, 'form[data-ack][data-open]');
    }

    function draftOf(form) {
      var area = form.querySelector('textarea');
      return area ? area.value : '';
    }

    function openDrafts() {
      return openForms().filter(function (form) {
        return draftOf(form).trim() !== '';
      });
    }

    function rowOf(el) {
      return el.closest('li.ltd-row');
    }

    // -------------------------------------------------------------- //
    // holds, timers and the freshness line

    function holdReason() {
      if (st.submitting > 0) return 'submitting';
      if (openForms().length > 0) return 'editing';
      if (hiddenNow()) return 'hidden';
      return null;
    }

    function clearTimers() {
      if (st.timer !== null) clearT(st.timer);
      if (st.ageTimer !== null) clearT(st.ageTimer);
      st.timer = null;
      st.ageTimer = null;
    }

    function schedule() {
      if (st.timer !== null) clearT(st.timer);
      st.timer = null;
      if (st.stopped || holdReason() !== null) return;
      st.timer = setT(tick, st.pollSeconds * 1000);
    }

    function tick() {
      st.timer = null;
      if (st.stopped) return;
      if (holdReason() !== null) {
        st.pendingResume = true;
        renderFreshness();
        return;
      }
      refresh({});
    }

    // Called whenever something that can hold the refresh changes.
    function syncHold() {
      if (st.stopped) return;
      if (holdReason() !== null) {
        if (st.timer !== null) clearT(st.timer);
        st.timer = null;
        st.pendingResume = true;
        renderFreshness();
        return;
      }
      renderFreshness();
      if (st.pendingResume) {
        st.pendingResume = false;
        refresh({});
      } else if (st.active === null) {
        schedule();
      }
    }

    function freshStatus() {
      if (st.stopped) return 'stopped';
      var reason = holdReason();
      if (reason === 'hidden') return 'hidden';
      if (reason !== null) return 'held';
      if (st.loading) return 'loading';
      if (st.failed) return 'failed';
      return 'ok';
    }

    function statusText(status) {
      switch (status) {
        case 'ok':
          return label('fresh_ok_template', { seconds: st.pollSeconds });
        case 'loading':
          return label('fresh_loading');
        case 'held':
          return label('fresh_held');
        case 'hidden':
          return label('fresh_hidden');
        case 'failed':
          return label('fresh_failed');
        default:
          return label('fresh_stopped');
      }
    }

    function minutesStale() {
      return Math.floor((now() - st.receivedAt) / 60000);
    }

    function renderFreshness() {
      if (st.ageTimer !== null) clearT(st.ageTimer);
      st.ageTimer = null;
      var fresh = rootEl.querySelector(HOOKS.fresh);
      if (fresh === null) return;
      var status = freshStatus();
      var aging = status === 'held' || status === 'hidden' || status === 'failed';
      toggleClass(fresh, 'is-held', status === 'held' || status === 'hidden');
      toggleClass(fresh, 'is-fail', status === 'failed' || status === 'stopped');
      toggleClass(rootEl, 'is-stale', st.failed && !st.stopped);
      all(rootEl, HOOKS.row).forEach(function (row) {
        toggleClass(row, 'is-stale', st.failed && !st.stopped);
      });

      // The line is a live region: write it only when the words change.
      var auto = rootEl.querySelector(HOOKS.statusAuto);
      var text = statusText(status);
      if (auto !== null && text && auto.textContent !== text) {
        auto.textContent = text;
      }
      var line = rootEl.querySelector(HOOKS.status);
      var spin = line !== null ? line.querySelector('.ltd-spin') : null;
      if (line !== null && status === 'loading' && spin === null) {
        line.insertBefore(
          make('span', { class: 'ltd-spin', 'aria-hidden': 'true' }),
          line.firstChild
        );
      } else if (status !== 'loading' && spin !== null) {
        removeNode(spin);
      }

      var minutes = minutesStale();
      var ago = fresh.querySelector('.ltd-ago');
      if (aging && minutes >= 1) {
        var agoText = ' ' + label('fresh_ago_template', { minutes: minutes });
        if (ago === null) {
          var zone = fresh.querySelector('.ltd-tz');
          var host = zone ? zone.parentNode : fresh.querySelector('p');
          if (host) {
            ago = make('span', { class: 'ltd-ago' });
            host.insertBefore(ago, zone ? zone.nextSibling : null);
          }
        }
        if (ago !== null && ago.textContent !== agoText) {
          ago.textContent = agoText;
        }
      } else if (ago !== null) {
        removeNode(ago);
      }

      var button = rootEl.querySelector(HOOKS.refreshButton);
      if (button !== null) {
        var retry = status === 'failed';
        var wanted = retry ? label('refresh_retry') : label('refresh_now');
        if (button.textContent !== wanted) button.textContent = wanted;
        toggleClass(button, 'pri', retry);
      }
      if (aging && !st.stopped) {
        st.ageTimer = setT(renderFreshness, AGE_TICK_MS);
      }
    }

    // -------------------------------------------------------------- //
    // notices

    function announce(text) {
      var live = rootEl.querySelector(HOOKS.live);
      if (live !== null && text) live.textContent = text;
    }

    function clearNotice(name) {
      all(rootEl, '[' + JS_MARK + '="' + name + '"]').forEach(removeNode);
    }

    // The slot of the panel's own banner: below the filters.
    function insertInSlot(node) {
      var anchor =
        rootEl.querySelector(HOOKS.filters) || rootEl.querySelector(HOOKS.top);
      if (anchor !== null && anchor.parentNode === rootEl) {
        rootEl.insertBefore(node, anchor.nextSibling);
      } else {
        rootEl.insertBefore(node, rootEl.firstChild);
      }
    }

    function banner(name, kind, heading, details, actions, anchor) {
      clearNotice(name);
      var box = make('div', {
        class: 'ltd-ban b-' + kind,
        role: kind === 'err' ? 'alert' : 'status',
        tabindex: '-1'
      });
      box.setAttribute(JS_MARK, name);
      var head = make('p', { class: 'ltd-banh' });
      head.appendChild(
        make(
          'i',
          { 'aria-hidden': 'true' },
          kind === 'info' ? 'i' : kind === 'ok' ? '✓' : '!'
        )
      );
      head.appendChild(doc.createTextNode(heading));
      box.appendChild(head);
      if (details && details.length) {
        var body = make('div', { class: 'ltd-banb' });
        details.forEach(function (detail) {
          body.appendChild(make('p', {}, detail));
        });
        box.appendChild(body);
      }
      if (actions && actions.length) {
        var row = make('div', { class: 'ltd-frow' });
        actions.forEach(function (action) {
          row.appendChild(action);
        });
        box.appendChild(row);
      }
      if (anchor) anchor.parentNode.insertBefore(box, anchor.nextSibling);
      else insertInSlot(box);
      return box;
    }

    function shownTime() {
      var time = rootEl.querySelector(HOOKS.freshTime);
      return time ? time.textContent.trim() : '';
    }

    function showFailure() {
      if (rootEl.querySelector('[' + JS_MARK + '="fail"]') !== null) return;
      var retry = make(
        'button',
        { type: 'button', class: 'ltd-btn pri' },
        label('refresh_retry')
      );
      retry.setAttribute(JS_MARK + '-retry', '');
      banner(
        'fail',
        'err',
        label('fresh_failed'),
        [label('failure_detail_template', { time: shownTime() })],
        [retry]
      );
    }

    // The last notice of the panel's slot, or what the slot follows.
    function lastNotice() {
      var last = null;
      Array.prototype.forEach.call(rootEl.childNodes, function (node) {
        if (node.nodeType === 1 && node.hasAttribute(JS_MARK)) last = node;
      });
      return (
        last || rootEl.querySelector(HOOKS.filters) || rootEl.querySelector(HOOKS.top)
      );
    }

    function draftCard(text) {
      var count = all(rootEl, '.ltd-draft').length;
      var id = count === 0 ? 'ltd-draft' : 'ltd-draft-' + (count + 1);
      var card = make('div', { class: 'ltd-empty ltd-draft' });
      card.setAttribute(JS_MARK, 'draft');
      card.appendChild(make('h2', {}, label('draft_heading')));
      card.appendChild(make('p', {}, label('draft_detail')));
      card.appendChild(
        make('label', { class: 'ltd-lbl', for: id }, label('draft_readonly'))
      );
      var area = make('textarea', { id: id, readonly: '', rows: '2' });
      area.value = text;
      area.textContent = text;
      card.appendChild(area);
      return card;
    }

    function addDraftCards(texts, after) {
      var anchor = after;
      texts.forEach(function (text) {
        var card = draftCard(text);
        if (anchor) {
          anchor.parentNode.insertBefore(card, anchor.nextSibling);
        } else {
          var slot = lastNotice();
          rootEl.insertBefore(card, slot ? slot.nextSibling : null);
        }
        anchor = card;
      });
    }

    function formMessage(form, text, detail) {
      all(form, '[' + JS_MARK + '="form"]').forEach(removeNode);
      var box = make('div', { class: 'ltd-fmsg is-err', role: 'alert' });
      box.setAttribute(JS_MARK, 'form');
      box.appendChild(make('b', {}, text));
      if (detail) box.appendChild(make('p', {}, detail));
      form.insertBefore(box, form.firstChild);
    }

    function pinMessage(row, text, ok) {
      all(row, '[' + JS_MARK + '="pin"]').forEach(removeNode);
      var form = row.querySelector(HOOKS.pinForm);
      if (form === null || !text) return;
      var note = make(
        'p',
        { class: ok ? 'ltd-okmsg' : 'ltd-err', role: ok ? 'status' : 'alert' },
        text
      );
      note.setAttribute(JS_MARK, 'pin');
      form.parentNode.insertBefore(note, form.nextSibling);
    }

    function okMessage(row, text) {
      all(row, '[' + JS_MARK + '="ok"]').forEach(removeNode);
      var band = row.querySelector(HOOKS.ackBand);
      if (band === null || !text) return;
      var note = make('p', { class: 'ltd-okmsg', role: 'status' }, text);
      note.setAttribute(JS_MARK, 'ok');
      band.insertBefore(note, band.firstChild);
    }

    // -------------------------------------------------------------- //
    // enhancing the markup (idempotent; handlers are delegated, so a
    // replaced panel never binds anything twice)

    function enhance() {
      toggleClass(rootEl, 'is-js', true);
      all(rootEl, HOOKS.opener).forEach(function (opener) {
        setHidden(opener, false);
      });
      all(rootEl, HOOKS.ackForm).forEach(function (form) {
        var open = form.hasAttribute('data-open');
        var cancel = form.querySelector(HOOKS.cancel);
        if (cancel !== null) setHidden(cancel, !open);
        if (open) {
          var opener = openerOf(form);
          if (opener !== null) opener.setAttribute('aria-expanded', 'true');
        }
      });
    }

    function syncRootAttributes(fresh) {
      Array.prototype.slice.call(fresh.attributes).forEach(function (attr) {
        var value = attr.name === 'class' ? attr.value + ' is-js' : attr.value;
        if (rootEl.getAttribute(attr.name) !== value) {
          rootEl.setAttribute(attr.name, value);
        }
      });
    }

    // -------------------------------------------------------------- //
    // the check form

    function openerOf(form) {
      var row = rowOf(form);
      if (row === null || !form.id) return null;
      return row.querySelector(
        'button[data-ack-open][aria-controls="' + form.id + '"]'
      );
    }

    function formOfRow(rowId) {
      var row = byId(rowId);
      return row ? row.querySelector(HOOKS.ackForm) : null;
    }

    function commentMax(form) {
      var counter = form.querySelector('.ltd-cnt');
      var found = counter ? /\/\s*(\d+)/.exec(counter.textContent) : null;
      return found ? Number(found[1]) : COMMENT_MAX;
    }

    function updateCounter(form) {
      var area = form.querySelector('textarea');
      var counter = form.querySelector('.ltd-cnt');
      if (area === null || counter === null) return;
      var max = commentMax(form);
      var count = codePoints(area.value);
      counter.textContent = label('ack_counter_template', {
        count: count,
        max: max
      });
      toggleClass(counter, 'is-over', count > max);
    }

    function setFormOpen(form, open) {
      setFlag(form, 'data-open', open);
      var opener = openerOf(form);
      if (opener !== null) {
        opener.setAttribute('aria-expanded', open ? 'true' : 'false');
      }
      var cancel = form.querySelector(HOOKS.cancel);
      if (cancel !== null) setHidden(cancel, !open);
    }

    function isOpenWith(form, draft) {
      return form.hasAttribute('data-open') && draftOf(form) === draft;
    }

    function openFormWithDraft(form, draft) {
      setFormOpen(form, true);
      var area = form.querySelector('textarea');
      if (area !== null) area.value = draft;
      updateCounter(form);
    }

    function openFromButton(opener) {
      var form = byId(opener.getAttribute('aria-controls'));
      if (form === null) return;
      setFormOpen(form, true);
      syncHold();
      var area = form.querySelector('textarea');
      if (area !== null) area.focus();
    }

    function cancelForm(button) {
      var form = button.closest(HOOKS.ackForm);
      if (form === null) return;
      var opener = openerOf(form);
      setFormOpen(form, false);
      var area = form.querySelector('textarea');
      if (area !== null) area.value = '';
      updateCounter(form);
      all(form, '[' + JS_MARK + ']').forEach(removeNode);
      clearNotice('csrf');
      if (openDrafts().length === 0) {
        st.confirming = false;
        clearNotice('confirm');
      }
      syncHold();
      if (opener !== null) opener.focus();
    }

    function setHistory(button, open) {
      button.setAttribute('aria-expanded', open ? 'true' : 'false');
      var list = byId(button.getAttribute('aria-controls'));
      if (list !== null) setHidden(list, !open);
      var first = button.firstChild;
      if (first && first.nodeType === 3) {
        first.nodeValue =
          label(open ? 'history_hide' : 'history_show') + ' ';
      }
    }

    // -------------------------------------------------------------- //
    // focus

    function describeFocus() {
      var active = doc.activeElement;
      if (!active || active === doc.body || !rootEl.contains(active)) {
        return null;
      }
      var row = rowOf(active);
      if (row !== null) {
        var role = 'link';
        if (active.closest('button[data-ack-open]')) role = 'opener';
        else if (active.closest('button[data-ack-cancel]')) role = 'cancel';
        else if (active.closest(HOOKS.ackForm)) {
          role = active.closest('textarea') ? 'comment' : 'submit';
        } else if (active.closest(HOOKS.pinForm)) role = 'pin';
        else if (active.closest('button[data-hist]')) role = 'history';
        else if (active.closest('.ltd-rec')) role = 'record';
        else if (active.closest('h3 a')) role = 'mlink';
        else if (active.closest('h3')) role = 'heading';
        var link = active.closest('a[href]');
        return {
          rowId: row.id,
          role: role,
          href: role === 'link' && link ? link.getAttribute('href') : null
        };
      }
      var tile = active.closest('li.ltd-st');
      if (tile !== null) return { tile: tierLetter(tile) };
      if (active.closest(HOOKS.refreshButton)) return { role: 'refresh' };
      if (active.id) return { id: active.id };
      return null;
    }

    function headingOf(row) {
      var heading = row ? row.querySelector('h3') : null;
      if (heading !== null && !heading.hasAttribute('tabindex')) {
        heading.setAttribute('tabindex', '-1');
      }
      return heading;
    }

    function focusRow(row) {
      var heading = headingOf(row);
      if (heading === null) return false;
      toggleClass(row, 'is-focus', true);
      heading.focus();
      return true;
    }

    var ROLE_SELECTORS = {
      opener: 'button[data-ack-open]',
      cancel: 'button[data-ack-cancel]',
      comment: 'form[data-ack][data-open] textarea',
      submit: 'form[data-ack][data-open] button[type="submit"]',
      pin: 'form.ltd-pin button[data-pin]',
      history: 'button[data-hist]',
      record: '.ltd-rec',
      mlink: 'h3 a'
    };

    // Put the focus back where it was. A row that is gone hands it to the
    // next row's heading, else the previous one, else the count heading.
    function restoreFocus(desc, before) {
      if (!desc) return null;
      var target = null;
      if (desc.tile) {
        target = rootEl.querySelector(
          '.ltd-sum li.ltd-st.t-' + desc.tile + ' a[href]'
        );
        if (target === null) target = countHeading();
      } else if (desc.role === 'refresh') {
        target = rootEl.querySelector(HOOKS.refreshButton);
      } else if (desc.id) {
        target = byId(desc.id);
      } else if (desc.rowId) {
        var row = byId(desc.rowId);
        if (row !== null) {
          var selector = ROLE_SELECTORS[desc.role];
          if (desc.role === 'heading') target = headingOf(row);
          else if (selector) target = row.querySelector(selector);
          else if (desc.href) {
            target = row.querySelector('a[href="' + desc.href + '"]');
          }
          if (target === null) target = headingOf(row);
          if (target !== null) {
            target.focus();
            return null;
          }
        }
        return moveFocusFromGoneRow(desc.rowId, before);
      }
      if (target !== null) {
        if (!target.hasAttribute('href') && !target.hasAttribute('tabindex')) {
          if (!/^(button|a|textarea|input|select)$/i.test(target.tagName)) {
            target.setAttribute('tabindex', '-1');
          }
        }
        target.focus();
      }
      return null;
    }

    function countHeading() {
      var heading = rootEl.querySelector(HOOKS.countHeading);
      if (heading !== null) heading.setAttribute('tabindex', '-1');
      return heading;
    }

    function moveFocusFromGoneRow(rowId, before) {
      var order = before.rows;
      var at = order.indexOf(rowId);
      var next = null;
      var to = 'count';
      var i;
      for (i = at + 1; at >= 0 && i < order.length && next === null; i++) {
        if (byId(order[i]) !== null) {
          next = byId(order[i]);
          to = 'next';
        }
      }
      for (i = at - 1; at >= 0 && i >= 0 && next === null; i--) {
        if (byId(order[i]) !== null) {
          next = byId(order[i]);
          to = 'previous';
        }
      }
      if (next !== null) focusRow(next);
      else {
        var heading = countHeading();
        if (heading !== null) heading.focus();
      }
      return { to: to, match: before.gone };
    }

    // -------------------------------------------------------------- //
    // replacing the panel

    function takeSnapshot(spec) {
      var snap = {
        focus: spec.focus !== undefined ? spec.focus : describeFocus(),
        histories: all(rootEl, 'button[data-hist][aria-expanded="true"]').map(
          function (button) {
            return button.getAttribute('aria-controls');
          }
        ),
        rows: all(rootEl, HOOKS.row).map(function (row) {
          return row.id;
        }),
        forms: [],
        gone: ''
      };
      var focusRowId = snap.focus && snap.focus.rowId;
      if (focusRowId) {
        var row = byId(focusRowId);
        var tournament = row ? row.querySelector(HOOKS.rowContext) : null;
        var where = row ? row.querySelector(HOOKS.rowLocation) : null;
        snap.gone = [tournament, where]
          .filter(Boolean)
          .map(function (el) {
            return el.textContent.trim();
          })
          .join(' · ');
      }
      if (spec.keepForms) {
        openForms().forEach(function (form) {
          if (form === spec.skip) return;
          var row = rowOf(form);
          snap.forms.push({
            rowId: row ? row.id : null,
            draft: draftOf(form),
            target: true
          });
        });
      }
      if (spec.forms) snap.forms = snap.forms.concat(spec.forms);
      return snap;
    }

    // What the server's markup said at the last swap, by unit and by row:
    // the script's own state in the live nodes never takes part, so a node
    // stays when the server has nothing new to say about it.
    function describePanel(panel) {
      var shown = { units: Object.create(null), rows: Object.create(null) };
      keyedChildren(panel, unitKey).forEach(function (unit) {
        var node = unit.node;
        var skip = null;
        if (isA(node, HOOKS.rows) || isA(node, HOOKS.live)) skip = always;
        else if (isA(node, HOOKS.top)) skip = isVolatile;
        shown.units[unit.key] = signature(node, skip);
        if (isA(node, HOOKS.rows)) {
          keyedChildren(node, idKey).forEach(function (row) {
            shown.rows[row.key] = describeRow(row.node);
          });
        }
      });
      return shown;
    }

    // The freshness line carries a time and a status of its own.
    function isVolatile(el) {
      return (
        isA(el, HOOKS.freshTime) ||
        isA(el, HOOKS.freshZone) ||
        isA(el, HOOKS.statusAuto)
      );
    }

    function articleOf(row) {
      return row.querySelector(HOOKS.article);
    }

    // A row is its shell and the parts of its article (tier, main, times,
    // actions, check): each part is kept or replaced on its own.
    function describeRow(row) {
      var article = articleOf(row);
      var parts = Object.create(null);
      if (article !== null) {
        keyedChildren(article, unitKey).forEach(function (part) {
          parts[part.key] = signature(part.node);
        });
      }
      var loose = shellOf(row) + (article !== null ? shellOf(article) : '');
      return {
        loose: loose,
        parts: parts,
        sig: loose + JSON.stringify(parts)
      };
    }

    function mergeParts(liveRow, freshRow, was, now) {
      var liveArticle = articleOf(liveRow);
      var freshArticle = articleOf(freshRow);
      if (liveArticle === null || freshArticle === null) return false;
      var current = Object.create(null);
      keyedChildren(liveArticle, unitKey).forEach(function (part) {
        current[part.key] = part.node;
      });
      arrange(
        liveArticle,
        keyedChildren(freshArticle, unitKey).map(function (part) {
          var live = current[part.key];
          return live !== undefined && was.parts[part.key] === now.parts[part.key]
            ? live
            : part.node;
        })
      );
      return true;
    }

    // Insert the rows that came, drop the rows that went, keep the server's
    // order, and touch a row that stays only where the server changed it.
    function mergeRows(liveList, freshList, prev, next, force) {
      var current = Object.create(null);
      keyedChildren(liveList, idKey).forEach(function (row) {
        current[row.key] = row.node;
      });
      arrange(
        liveList,
        keyedChildren(freshList, idKey).map(function (row) {
          var live = current[row.key];
          var was = prev.rows[row.key];
          var now = next.rows[row.key];
          if (live === undefined || was === undefined) return row.node;
          if (force.indexOf(row.key) >= 0) return row.node;
          if (was.sig === now.sig) return live;
          if (was.loose === now.loose && mergeParts(live, row.node, was, now)) {
            return live;
          }
          return row.node;
        })
      );
    }

    function syncFreshness(liveTop, freshTop) {
      [HOOKS.freshTime, HOOKS.freshZone].forEach(function (selector) {
        var shown = liveTop.querySelector(selector);
        var wanted = freshTop.querySelector(selector);
        if (
          shown !== null &&
          wanted !== null &&
          shown.textContent !== wanted.textContent
        ) {
          shown.textContent = wanted.textContent;
        }
      });
    }

    // Bring the live panel to the server's, keeping every node the server
    // did not change. The script's own notices are swept first: they last
    // until the next swap.
    function mergePanel(fresh, next, force) {
      var prev = st.shown;
      all(rootEl, '[' + JS_MARK + ']').forEach(removeNode);
      var current = Object.create(null);
      keyedChildren(rootEl, unitKey).forEach(function (unit) {
        current[unit.key] = unit.node;
      });
      arrange(
        rootEl,
        keyedChildren(fresh, unitKey).map(function (unit) {
          var live = current[unit.key];
          if (live === undefined) return unit.node;
          if (prev.units[unit.key] !== next.units[unit.key]) return unit.node;
          if (isA(live, HOOKS.top)) syncFreshness(live, unit.node);
          else if (isA(live, HOOKS.rows)) {
            mergeRows(live, unit.node, prev, next, force);
          } else if (isA(live, HOOKS.live) && live.textContent !== '') {
            live.textContent = '';
          }
          return live;
        })
      );
      st.shown = next;
    }

    // Swap the panel's content for a newer snapshot. Returns what the
    // caller may need: whether it worked, and the drafts that lost their
    // row.
    function applyFragment(fragment, spec) {
      spec = spec || {};
      if (fragment.asOf < st.shownAsOf) return { ok: false, reason: 'older' };
      var fresh = parseFragment(fragment.html);
      if (
        !fresh ||
        !fresh.attributes ||
        !fresh.hasAttribute('data-lt-dashboard-root')
      ) {
        return { ok: false, reason: 'invalid' };
      }
      var before = takeSnapshot(spec);
      var focused = doc.activeElement;
      if (spec.freshQuery) dirty = {};
      syncRootAttributes(fresh);
      mergePanel(fresh, describePanel(fresh), spec.forceRows || []);
      labels = readLabels(rootEl);
      st.shownAsOf = fragment.asOf;
      st.receivedAt = now();
      if (fragment.pollSeconds >= MIN_POLL_SECONDS) {
        st.pollSeconds = fragment.pollSeconds;
      }
      st.failed = false;
      st.pendingResume = false;
      st.confirming = false;
      enhance();
      restoreChoices();

      before.histories.forEach(function (id) {
        var button = rootEl.querySelector(
          'button[data-hist][aria-controls="' + id + '"]'
        );
        if (button !== null && button.getAttribute('aria-expanded') !== 'true') {
          setHistory(button, true);
        }
      });
      var unsent = [];
      before.forms.forEach(function (item) {
        var form = item.target && item.rowId ? formOfRow(item.rowId) : null;
        if (form !== null) {
          // A form the swap left alone is open with its draft already.
          if (!isOpenWith(form, item.draft)) openFormWithDraft(form, item.draft);
        } else if (item.draft.trim() !== '') unsent.push(item.draft);
      });
      // A control that is still where it was keeps its focus, and with it
      // the caret and the reading position.
      var kept =
        spec.focus === undefined &&
        focused !== null &&
        focused !== doc.body &&
        rootEl.contains(focused) &&
        doc.activeElement === focused;
      var moved = kept ? null : restoreFocus(before.focus, before);
      if (moved !== null) {
        var details = moved.to === 'next' ? [label('focus_next')] : [];
        banner(
          'focus',
          'info',
          label('focus_moved_template', { match: moved.match }),
          details
        );
      }
      if (unsent.length) addDraftCards(unsent);
      renderFreshness();
      return { ok: true, moved: moved, unsent: unsent, before: before };
    }

    // -------------------------------------------------------------- //
    // requests

    function readResponse(response) {
      var headers = response.headers;
      var type =
        headers && headers.get ? headers.get('Content-Type') || '' : '';
      return response.text().then(
        function (text) {
          var json = null;
          if (/json/i.test(type)) {
            try {
              var parsed = JSON.parse(text);
              if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
                json = parsed;
              }
            } catch (e) {
              json = null;
            }
          }
          return {
            status: response.status,
            redirected: !!response.redirected,
            url: response.url || '',
            json: json
          };
        },
        function () {
          return { failure: 'network' };
        }
      );
    }

    // One request; resolves with a plain result, never rejects. `req.abort`
    // lets the caller drop it for a newer one.
    function send(req, spec) {
      var controller = AC !== null ? new AC() : null;
      var timedOut = false;
      var timer = setT(function () {
        timedOut = true;
        if (controller !== null) controller.abort();
      }, timeoutMs);
      req.abort = function () {
        req.aborted = true;
        clearT(timer);
        if (controller !== null) controller.abort();
      };
      var init = {
        method: spec.method,
        credentials: 'same-origin',
        cache: 'no-store',
        redirect: 'follow',
        headers: { Accept: 'application/json' }
      };
      if (controller !== null) init.signal = controller.signal;
      if (spec.body !== undefined) {
        init.body = spec.body;
        init.headers['Content-Type'] =
          'application/x-www-form-urlencoded; charset=UTF-8';
      }
      var started;
      try {
        started = Promise.resolve(fetchFn(spec.url, init));
      } catch (e) {
        started = Promise.reject(e);
      }
      return started
        .then(readResponse, function () {
          return { failure: 'network' };
        })
        .then(function (result) {
          clearT(timer);
          if (result.failure && timedOut) result.failure = 'timeout';
          return result;
        });
    }

    function abortActive() {
      if (st.active !== null) {
        st.active.abort();
        st.active = null;
      }
      st.loading = false;
    }

    // Fetch the panel again. One request at a time: asking while one runs
    // joins it, unless the new request is for another query or has to keep
    // a draft.
    function refresh(opts) {
      opts = opts || {};
      if (st.stopped) return Promise.resolve({ status: 'stopped' });
      var distinct = !!(opts.url || opts.keepForms);
      if (st.active !== null && !distinct) {
        if (opts.manual && !st.active.manual) {
          st.active.manual = true;
          st.loading = true;
          renderFreshness();
        }
        return st.active.promise;
      }
      abortActive();
      if (st.timer !== null) clearT(st.timer);
      st.timer = null;
      var req = {
        id: ++st.sequence,
        manual: !!opts.manual,
        keepForms: !!opts.keepForms,
        tile: opts.tile || null,
        pushUrl: opts.pushUrl || null,
        aborted: false,
        abort: function () {},
        promise: null
      };
      st.active = req;
      st.loading = req.manual;
      if (req.manual) renderFreshness();
      req.promise = send(req, {
        method: 'GET',
        url: opts.url || pollUrl()
      }).then(function (result) {
        return finishPoll(req, result);
      });
      return req.promise;
    }

    function finishPoll(req, result) {
      if (req.aborted || st.active !== req) return { status: 'superseded' };
      st.active = null;
      st.loading = false;
      var outcome = classifyResponse(result, loginPath());
      switch (outcome.kind) {
        case 'ok':
          return pollSucceeded(req, outcome.data);
        case 'session':
          lose('session');
          return { status: 'session' };
        case 'access':
          lose('access');
          return { status: 'access' };
        case 'csrf':
          banner('csrf', 'err', label('csrf_invalid'), []);
          renderFreshness();
          schedule();
          return { status: 'csrf' };
        default:
          return pollFailed(req);
      }
    }

    function pollSucceeded(req, data) {
      var fragment = readFragment(data);
      if (fragment === null) return pollFailed(req);
      if (!req.keepForms && (st.submitting > 0 || openForms().length > 0)) {
        // A form opened while the request ran: it must not be replaced.
        st.pendingResume = true;
        renderFreshness();
        return { status: 'held' };
      }
      var recovered = st.failed;
      var applied = applyFragment(fragment, {
        keepForms: req.keepForms,
        focus: req.tile ? { tile: req.tile } : undefined,
        freshQuery: !!req.tile
      });
      if (!applied.ok) {
        if (applied.reason === 'older') {
          renderFreshness();
          schedule();
          return { status: 'older' };
        }
        return pollFailed(req);
      }
      if (req.pushUrl && hist && hist.pushState) {
        hist.pushState({ ltDashboard: true }, '', req.pushUrl);
        st.navigated = true;
      }
      if (req.tile) announceTile(req.tile);
      else if (recovered || req.manual) {
        announce(label('live_refreshed_template', { time: shownTime() }));
      }
      syncHold();
      return { status: 'applied', recovered: recovered };
    }

    function pollFailed(req) {
      if (req.tile && req.pushUrl && loc && loc.assign) {
        loc.assign(req.pushUrl);
        return { status: 'navigated' };
      }
      st.failed = true;
      showFailure();
      renderFreshness();
      schedule();
      return { status: 'failed' };
    }

    function announceTile(letter) {
      var count = rootEl.querySelector(HOOKS.countHeading);
      var tile = rootEl.querySelector('.ltd-sum li.ltd-st.t-' + letter);
      var link = tile ? tile.querySelector('a[href]') : null;
      var active = tile !== null && tile.classList.contains('is-on');
      var text;
      if (active && labels.tile_filtered_template) {
        var name = tile.querySelector('.ltd-stn');
        text = label('tile_filtered_template', {
          tier: name ? name.textContent.trim() : '',
          count: count ? count.textContent.trim() : ''
        });
      } else if (!active && labels.tile_unfiltered_template) {
        text = label('tile_unfiltered_template', {
          count: count ? count.textContent.trim() : ''
        });
      } else if (link !== null) {
        text = link.getAttribute('aria-label') || '';
      }
      announce(text);
    }

    // -------------------------------------------------------------- //
    // losing the session or the access

    function stopAll() {
      st.stopped = true;
      abortActive();
      clearTimers();
      if (doc.removeEventListener) {
        doc.removeEventListener('visibilitychange', onVisibility);
      }
    }

    function collectDrafts(extra) {
      var drafts = [];
      all(rootEl, HOOKS.ackForm).forEach(function (form) {
        var text = draftOf(form);
        if (form.hasAttribute('data-open') && text.trim() !== '') {
          drafts.push(text);
        }
      });
      (extra || []).forEach(function (text) {
        if (text && text.trim() !== '' && drafts.indexOf(text) < 0) {
          drafts.push(text);
        }
      });
      return drafts;
    }

    // Rows and actions leave the DOM; a draft stays, read only, and is
    // never sent again. There is no retry and no sign-in on its own.
    function lose(kind, extraDrafts) {
      var drafts = collectDrafts(extraDrafts);
      var search = loc ? (loc.pathname || '') + (loc.search || '') : '';
      stopAll();
      Array.prototype.slice.call(rootEl.childNodes).forEach(function (node) {
        var keep =
          node.nodeType === 1 &&
          (node.classList.contains('ltd-top') ||
            node.classList.contains('ltd-sr'));
        if (!keep) rootEl.removeChild(node);
      });
      removeNode(rootEl.querySelector(HOOKS.scope));
      removeNode(rootEl.querySelector(HOOKS.refreshForm));
      renderFreshness();
      var link;
      var heading;
      var detail;
      if (kind === 'session') {
        var login = rootEl.getAttribute('data-login-url') || '';
        var joined = login + (login.indexOf('?') < 0 ? '?' : '&');
        var target = login
          ? sameOrigin(joined + 'next=' + encodeURIComponent(search))
          : null;
        link = target
          ? make(
              'a',
              {
                class: 'ltd-btn pri',
                href: target.pathname + target.search
              },
              label('sign_in')
            )
          : null;
        heading = label('session_expired_heading');
        detail = label('session_expired_detail');
      } else {
        var overview = rootEl.getAttribute('data-overview-url');
        var url = sameOrigin(
          overview ||
            overviewUrl(
              rootEl.getAttribute('data-surface'),
              pollUrl(),
              baseHref()
            )
        );
        link = url
          ? make(
              'a',
              { class: 'ltd-btn', href: url.pathname + url.search },
              label('to_tournament_overview')
            )
          : null;
        heading = label('access_lost_heading');
        detail = label('access_lost_detail');
      }
      var box = banner(
        'lost',
        'err',
        heading,
        [detail],
        link ? [link] : [],
        rootEl.querySelector(HOOKS.top)
      );
      addDraftCards(drafts, box);
      box.focus();
    }

    // -------------------------------------------------------------- //
    // the check and the pin

    function serializeForm(form) {
      var pairs = [];
      all(form, 'input[name], textarea[name]').forEach(function (field) {
        var type = (field.getAttribute('type') || '').toLowerCase();
        if (type === 'submit' || type === 'button') return;
        if (field.hasAttribute('disabled')) return;
        pairs.push(
          encodeURIComponent(field.getAttribute('name')) +
            '=' +
            encodeURIComponent(field.value === undefined ? '' : field.value)
        );
      });
      return pairs.join('&').replace(/%20/g, '+');
    }

    function setPending(form, kind, on) {
      var button =
        kind === 'ack'
          ? form.querySelector('button[type="submit"]')
          : form.querySelector(HOOKS.pinButton);
      if (button === null) return;
      if (on) {
        var original = button.textContent;
        if (pending !== null) pending.set(form, original);
        var wording =
          kind === 'ack'
            ? label('ack_saving')
            : label(
                form.querySelector('input[name="pinned"]') &&
                  form.querySelector('input[name="pinned"]').value === 'true'
                  ? 'pin_adding'
                  : 'pin_removing'
              );
        button.textContent = '';
        button.appendChild(
          make('span', { class: 'ltd-spin', 'aria-hidden': 'true' })
        );
        button.appendChild(doc.createTextNode(wording));
        button.setAttribute('disabled', '');
        button.setAttribute('aria-busy', 'true');
        if (kind === 'ack') {
          form.setAttribute('aria-busy', 'true');
          var area = form.querySelector('textarea');
          if (area !== null) area.setAttribute('readonly', '');
          var cancel = form.querySelector(HOOKS.cancel);
          if (cancel !== null) cancel.setAttribute('disabled', '');
          var buttons = form.querySelector('.ltd-frow');
          if (buttons !== null) {
            var note = make(
              'span',
              { class: 'ltd-pend', role: 'status' },
              label('ack_pending')
            );
            note.setAttribute(JS_MARK, 'pending');
            buttons.appendChild(note);
          }
        }
      } else {
        var saved = pending !== null ? pending.get(form) : undefined;
        button.textContent = saved === undefined ? '' : saved;
        button.removeAttribute('disabled');
        button.removeAttribute('aria-busy');
        if (kind === 'ack') {
          form.removeAttribute('aria-busy');
          var field = form.querySelector('textarea');
          if (field !== null) field.removeAttribute('readonly');
          var back = form.querySelector(HOOKS.cancel);
          if (back !== null) back.removeAttribute('disabled');
          all(form, '[' + JS_MARK + '="pending"]').forEach(removeNode);
        }
      }
    }

    // Send the viewer's own check or pin. Returns false when the form is
    // left to the browser: it does not post to this origin.
    function submitAction(form, kind) {
      var target = sameOrigin(form.getAttribute('action'));
      if (target === null) return false;
      var row = rowOf(form);
      var rowId = row ? row.id : null;
      var draft = kind === 'ack' ? draftOf(form) : null;
      var body = serializeForm(form);
      var focus = describeFocus();
      all(form, '[' + JS_MARK + ']').forEach(removeNode);
      if (row !== null) all(row, '[' + JS_MARK + '="pin"]').forEach(removeNode);
      clearNotice('csrf');
      abortActive();
      st.submitting++;
      setPending(form, kind, true);
      syncHold();
      var req = { aborted: false, abort: function () {} };
      send(req, {
        method: 'POST',
        url: target.pathname + target.search,
        body: body
      }).then(function (result) {
        st.submitting--;
        return actionDone({
          form: form,
          kind: kind,
          rowId: rowId,
          draft: draft,
          focus: focus,
          result: result
        });
      });
      return true;
    }

    function actionDone(ctx) {
      if (st.stopped) return { status: 'stopped' };
      var outcome = classifyResponse(ctx.result, loginPath());
      var form = ctx.form;
      switch (outcome.kind) {
        case 'ok':
          return actionSucceeded(ctx, outcome.data);
        case 'session':
          lose('session', [ctx.draft]);
          return { status: 'session' };
        case 'access':
          lose('access', [ctx.draft]);
          return { status: 'access' };
        case 'csrf':
          setPending(form, ctx.kind, false);
          banner('csrf', 'err', label('csrf_invalid'), []);
          syncHold();
          return { status: 'csrf' };
        case 'refusal':
          return actionRefused(ctx, outcome);
        default:
          setPending(form, ctx.kind, false);
          if (ctx.kind === 'ack') {
            formMessage(form, label('ack_failed') || label('fresh_failed'));
          } else {
            var row = rowOf(form);
            if (row !== null) pinMessage(row, label('pin_failed'), false);
          }
          syncHold();
          return { status: 'failed' };
      }
    }

    function actionSucceeded(ctx, data) {
      var fragment = readFragment(data.fragment);
      if (fragment === null) {
        // Saved, but the answer is unusable: read the server's state back.
        readBack(ctx);
        return { status: 'saved' };
      }
      var applied = applyFragment(fragment, {
        keepForms: true,
        skip: ctx.form,
        // The row that was acted on starts again from the server's markup.
        forceRows: ctx.rowId ? [ctx.rowId] : [],
        focus:
          ctx.kind === 'ack' ? { rowId: ctx.rowId, role: 'record' } : ctx.focus
      });
      if (!applied.ok) {
        readBack(ctx);
        return { status: 'saved' };
      }
      var row = ctx.rowId ? byId(ctx.rowId) : null;
      if (ctx.kind === 'ack') {
        if (row !== null) {
          var wait = row.querySelector(HOOKS.wait);
          okMessage(
            row,
            label('ack_success_template', {
              time: shownTime(),
              wait: wait ? wait.textContent.trim() : ''
            })
          );
        }
        announce(label('ack_announce'));
      } else {
        var signal = row ? row.querySelector(HOOKS.pinSignal) : null;
        var pinned = ctx.form.querySelector('input[name="pinned"]');
        var text =
          pinned && pinned.value === 'false'
            ? label('pin_removed')
            : signal !== null
              ? visibleText(signal).trim()
              : '';
        if (row !== null) pinMessage(row, text, true);
        announce(text);
      }
      syncHold();
      return { status: 'saved' };
    }

    function readBack(ctx) {
      setPending(ctx.form, ctx.kind, false);
      if (ctx.kind === 'ack') {
        setFormOpen(ctx.form, false);
        var area = ctx.form.querySelector('textarea');
        if (area !== null) area.value = '';
        updateCounter(ctx.form);
      }
      syncHold();
      refresh({ manual: true, keepForms: openForms().length > 0 });
    }

    function actionRefused(ctx, outcome) {
      var data = outcome.data || {};
      var message = typeof data.message === 'string' ? data.message : '';
      // The other orga's record, as the server words it for this viewer.
      var staleDetail =
        ctx.kind === 'ack' &&
        outcome.code === 'stale' &&
        typeof data.detail === 'string' &&
        data.detail !== ''
          ? data.detail
          : null;
      var fragment = readFragment(data.fragment);
      var form = ctx.form;
      if (fragment === null) {
        // 404 `unavailable`, or an answer without a panel: the row stays.
        setPending(form, ctx.kind, false);
        if (ctx.kind === 'ack') formMessage(form, message, staleDetail);
        else {
          var host = rowOf(form);
          if (host !== null) pinMessage(host, message, false);
        }
        syncHold();
        return { status: 'refused' };
      }
      var target = data.draft_target === true;
      var applied = applyFragment(fragment, {
        keepForms: true,
        skip: form,
        forceRows: ctx.rowId ? [ctx.rowId] : [],
        forms:
          ctx.kind === 'ack'
            ? [{ rowId: ctx.rowId, draft: ctx.draft, target: target }]
            : [],
        focus:
          ctx.kind === 'ack' && target
            ? { rowId: ctx.rowId, role: 'comment' }
            : ctx.focus
      });
      if (!applied.ok) {
        setPending(form, ctx.kind, false);
        if (ctx.kind === 'ack') formMessage(form, message, staleDetail);
        syncHold();
        return { status: 'refused' };
      }
      var detail = null;
      if (ctx.kind === 'ack') {
        var text =
          outcome.code === 'refused'
            ? label('ack_refused_template', { reason: message })
            : message;
        detail = outcome.code === 'refused' ? label('ack_refused_detail') : staleDetail;
        var fresh = target && ctx.rowId ? formOfRow(ctx.rowId) : null;
        if (fresh !== null) formMessage(fresh, text, detail);
        else {
          banner('refusal', 'err', text, detail ? [detail] : []);
        }
      } else {
        var row = ctx.rowId ? byId(ctx.rowId) : null;
        if (row !== null) pinMessage(row, message, false);
        else banner('refusal', 'err', message, []);
      }
      syncHold();
      return { status: 'refused', draftTarget: target };
    }

    // -------------------------------------------------------------- //
    // the manual refresh

    function manualRefresh() {
      if (st.stopped || st.confirming) return;
      if (openDrafts().length > 0) {
        askBeforeRefresh();
        return;
      }
      // An open form without a draft is kept as it is.
      refresh({ manual: true, keepForms: openForms().length > 0 });
    }

    function askBeforeRefresh() {
      st.confirming = true;
      var yes = make(
        'button',
        { type: 'button', class: 'ltd-btn pri' },
        label('ack_refresh_keep')
      );
      yes.setAttribute(JS_MARK + '-yes', '');
      var no = make(
        'button',
        { type: 'button', class: 'ltd-btn' },
        label('ack_cancel')
      );
      no.setAttribute(JS_MARK + '-no', '');
      var box = banner('confirm', 'warn', label('refresh_confirm'), [], [
        yes,
        no
      ]);
      box.setAttribute('role', 'group');
      yes.focus();
    }

    function closeConfirm(accept) {
      st.confirming = false;
      clearNotice('confirm');
      if (accept) {
        refresh({ manual: true, keepForms: true });
      } else {
        var button = rootEl.querySelector(HOOKS.refreshButton);
        if (button !== null) button.focus();
      }
    }

    // -------------------------------------------------------------- //
    // events (delegated on the root, bound once)

    function onClick(event) {
      var target = event.target;
      if (!target || !target.closest) return;
      var hit;
      if ((hit = target.closest('button[data-ack-open]')) !== null) {
        openFromButton(hit);
      } else if ((hit = target.closest('button[data-ack-cancel]')) !== null) {
        cancelForm(hit);
      } else if ((hit = target.closest('button[data-hist]')) !== null) {
        setHistory(hit, hit.getAttribute('aria-expanded') !== 'true');
      } else if ((hit = target.closest(HOOKS.refreshButton)) !== null) {
        // A stopped page leaves the refresh to the browser.
        if (st.stopped) return;
        event.preventDefault();
        manualRefresh();
      } else if (target.closest('[' + JS_MARK + '-retry]') !== null) {
        manualRefresh();
      } else if (target.closest('[' + JS_MARK + '-yes]') !== null) {
        closeConfirm(true);
      } else if (target.closest('[' + JS_MARK + '-no]') !== null) {
        closeConfirm(false);
      } else if ((hit = target.closest('.ltd-sum li.ltd-st a[href]')) !== null) {
        onTile(hit, event);
      }
    }

    function onTile(link, event) {
      if (st.stopped || event.defaultPrevented) return;
      if (event.button > 0) return;
      if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) {
        return;
      }
      // Leaving the page with an open form is the viewer's own choice.
      if (openForms().length > 0) return;
      var destination = sameOrigin(link.getAttribute('href'));
      var poll = sameOrigin(pollUrl());
      var letter = tierLetter(link.closest('li.ltd-st'));
      if (destination === null || poll === null || letter === null) return;
      event.preventDefault();
      refresh({
        url: poll.pathname + destination.search,
        pushUrl: destination.pathname + destination.search + destination.hash,
        tile: letter
      });
    }

    function onChange(event) {
      var target = event.target;
      if (!target || !target.closest) return;
      if (target.closest('form.ltd-filters select') !== null && target.id) {
        dirty[target.id] = target.value;
      } else if (target.closest('form.ltd-scope input[type="radio"]') !== null) {
        dirty['scope:' + target.getAttribute('name')] = target.value;
      }
    }

    function restoreChoices() {
      Object.keys(dirty).forEach(function (key) {
        if (key.indexOf('scope:') === 0) {
          all(rootEl, 'form.ltd-scope input[type="radio"]').forEach(function (radio) {
            if ('scope:' + radio.getAttribute('name') === key) {
              radio.checked = radio.value === dirty[key];
            }
          });
          return;
        }
        var select = byId(key);
        if (select === null) return;
        all(select, 'option').forEach(function (option) {
          if (option.getAttribute('value') === dirty[key]) {
            select.value = dirty[key];
          }
        });
      });
    }

    function onInput(event) {
      var target = event.target;
      if (!target || !target.closest || !target.closest('textarea')) return;
      var form = target.closest(HOOKS.ackForm);
      if (form !== null) updateCounter(form);
    }

    function onSubmit(event) {
      var form = event.target;
      if (!form || !form.hasAttribute || st.stopped) return;
      var kind = form.hasAttribute('data-ack')
        ? 'ack'
        : form.classList.contains('ltd-pin')
          ? 'pin'
          : null;
      if (kind !== null) {
        // One send at a time; a second submit is dropped, not queued.
        if (st.submitting > 0 || submitAction(form, kind)) {
          event.preventDefault();
        }
      } else if (form.classList.contains('ltd-refresh')) {
        event.preventDefault();
        manualRefresh();
      }
    }

    function onFocusOut() {
      all(rootEl, 'li.ltd-row.is-focus').forEach(function (row) {
        toggleClass(row, 'is-focus', false);
      });
    }

    function onVisibility() {
      if (st.stopped) return;
      syncHold();
    }

    function onPopState() {
      if (st.navigated && loc && loc.reload) loc.reload();
    }

    // -------------------------------------------------------------- //
    // start

    // The markup as the server rendered it, before the script touches it.
    st.shown = describePanel(rootEl);
    rootEl.addEventListener('click', onClick);
    rootEl.addEventListener('input', onInput);
    rootEl.addEventListener('change', onChange);
    rootEl.addEventListener('submit', onSubmit);
    rootEl.addEventListener('focusout', onFocusOut);
    doc.addEventListener('visibilitychange', onVisibility);
    if (win.addEventListener) win.addEventListener('popstate', onPopState);
    enhance();
    renderFreshness();
    if (holdReason() !== null) st.pendingResume = true;
    else schedule();

    var controller = {
      refresh: function (opts) {
        return refresh(opts || { manual: true });
      },
      stop: function () {
        stopAll();
        renderFreshness();
      },
      state: function () {
        return {
          status: freshStatus(),
          hold: holdReason(),
          stopped: st.stopped,
          failed: st.failed,
          inFlight: st.active !== null,
          pendingResume: st.pendingResume,
          shownAsOf: st.shownAsOf,
          pollSeconds: st.pollSeconds
        };
      }
    };
    if (controllers !== null) controllers.set(rootEl, controller);
    return controller;
  }

  function init(doc, options) {
    var found = [];
    var roots = doc.querySelectorAll(ROOT);
    for (var i = 0; i < roots.length; i++) {
      var merged = { document: doc };
      Object.keys(options || {}).forEach(function (key) {
        merged[key] = options[key];
      });
      var controller = initDashboard(roots[i], merged);
      if (controller !== null) found.push(controller);
    }
    return found;
  }

  return {
    init: init,
    initDashboard: initDashboard,
    classifyResponse: classifyResponse,
    overviewUrl: overviewUrl,
    fill: fill,
    codePoints: codePoints,
    HOOKS: HOOKS
  };
});
