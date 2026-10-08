'use strict';

const fs = require('node:fs');
const test = require('node:test');
const assert = require('node:assert');

const {
  initDashboard,
  classifyResponse,
  overviewUrl,
  fill,
  codePoints,
  HOOKS
} = require('../../byceps/static/behavior/lan_tournament_dashboard.js');

// ------------------------------------------------------------------ //
// A small DOM: an HTML parser, selectors, bubbling events and the focus
// rules of a browser (a node that cannot take focus ignores `focus()`).

const VOID = new Set(['input', 'br', 'hr', 'img', 'meta', 'link']);

function decode(text) {
  const table = { amp: '&', lt: '<', gt: '>', quot: '"', '#39': "'" };
  return text.replace(/&(amp|lt|gt|quot|#39);/g, (whole, name) => table[name]);
}

function splitTop(text, separators) {
  const parts = [];
  let depth = 0;
  let quote = null;
  let current = '';
  for (const char of text) {
    if (quote !== null) {
      if (char === quote) quote = null;
      current += char;
    } else if (char === '"' || char === "'") {
      quote = char;
      current += char;
    } else if (char === '[') {
      depth += 1;
      current += char;
    } else if (char === ']') {
      depth -= 1;
      current += char;
    } else if (depth === 0 && separators.includes(char)) {
      parts.push([current, char]);
      current = '';
    } else {
      current += char;
    }
  }
  parts.push([current, '']);
  return parts;
}

function parseCompound(text) {
  const out = { tag: null, id: null, classes: [], attrs: [] };
  const re = /^(\*|[a-zA-Z][\w-]*)|#([\w-]+)|\.([\w-]+)|\[([\w-]+)(?:=(?:"([^"]*)"|'([^']*)'|([^\]]*)))?\]/y;
  let at = 0;
  while (at < text.length) {
    re.lastIndex = at;
    const m = re.exec(text);
    assert.ok(m, `unsupported selector part: ${text.slice(at)}`);
    if (m[1] !== undefined) out.tag = m[1] === '*' ? null : m[1].toUpperCase();
    else if (m[2] !== undefined) out.id = m[2];
    else if (m[3] !== undefined) out.classes.push(m[3]);
    else {
      const value = m[5] !== undefined ? m[5] : m[6] !== undefined ? m[6] : m[7];
      out.attrs.push([m[4], value]);
    }
    at = re.lastIndex;
  }
  return out;
}

function parseSelector(selector) {
  return splitTop(selector, ',').map(([alternative]) => {
    const chain = [];
    let comb = ' ';
    let buffer = '';
    const flush = () => {
      if (buffer !== '') chain.push({ comb, compound: parseCompound(buffer) });
      buffer = '';
    };
    let depth = 0;
    let quote = null;
    for (const char of alternative.trim()) {
      if (quote !== null) {
        if (char === quote) quote = null;
        buffer += char;
      } else if (char === '"' || char === "'") {
        quote = char;
        buffer += char;
      } else if (char === '[') {
        depth += 1;
        buffer += char;
      } else if (char === ']') {
        depth -= 1;
        buffer += char;
      } else if (depth === 0 && char === '>') {
        flush();
        comb = '>';
      } else if (depth === 0 && /\s/.test(char)) {
        if (buffer !== '') {
          flush();
          comb = ' ';
        }
      } else {
        buffer += char;
      }
    }
    flush();
    return chain;
  });
}

function matchCompound(node, c) {
  if (node.nodeType !== 1) return false;
  if (c.tag !== null && node.tagName !== c.tag) return false;
  if (c.id !== null && node.getAttribute('id') !== c.id) return false;
  if (!c.classes.every((name) => node.classList.contains(name))) return false;
  return c.attrs.every(([name, value]) => {
    const actual = node.getAttribute(name);
    return value === undefined ? actual !== null : actual === value;
  });
}

function matchChain(node, chain, i) {
  if (!matchCompound(node, chain[i].compound)) return false;
  if (i === 0) return true;
  if (chain[i].comb === '>') {
    const p = node.parentNode;
    return !!p && p.nodeType === 1 && matchChain(p, chain, i - 1);
  }
  for (let p = node.parentNode; p && p.nodeType === 1; p = p.parentNode) {
    if (matchChain(p, chain, i - 1)) return true;
  }
  return false;
}

class FakeText {
  constructor(doc, text) {
    this.nodeType = 3;
    this.ownerDocument = doc;
    this.nodeValue = text;
    this.parentNode = null;
  }
  get textContent() {
    return this.nodeValue;
  }
}

class FakeElement {
  constructor(doc, tag) {
    this.nodeType = 1;
    this.ownerDocument = doc;
    this.tagName = String(tag).toUpperCase();
    this.attrs = new Map();
    this.childNodes = [];
    this.parentNode = null;
    this.listeners = {};
    this._value = null;
    this._checked = null;
    const names = () => (this.getAttribute('class') || '').split(/\s+/).filter(Boolean);
    const write = (list) => this.setAttribute('class', list.join(' '));
    this.classList = {
      add: (name) => names().includes(name) || write(names().concat(name)),
      remove: (name) => write(names().filter((x) => x !== name)),
      contains: (name) => names().includes(name)
    };
  }
  get id() {
    return this.getAttribute('id') || '';
  }
  get attributes() {
    return Array.from(this.attrs, ([name, value]) => ({ name, value }));
  }
  get firstChild() {
    return this.childNodes[0] || null;
  }
  get nextSibling() {
    if (this.parentNode === null) return null;
    const siblings = this.parentNode.childNodes;
    return siblings[siblings.indexOf(this) + 1] || null;
  }
  get textContent() {
    return this.childNodes.map((n) => n.textContent).join('');
  }
  set textContent(value) {
    this.writes = (this.writes || 0) + 1;
    this.childNodes.forEach((n) => (n.parentNode = null));
    this.childNodes = [];
    if (String(value) !== '') this.appendChild(this.ownerDocument.createTextNode(String(value)));
  }
  get value() {
    if (this._value !== null) return this._value;
    if (this.tagName === 'TEXTAREA') return this.textContent;
    if (this.tagName === 'SELECT') {
      const options = this.querySelectorAll('option');
      const chosen = options.find((o) => o.hasAttribute('selected')) || options[0];
      return chosen ? chosen.getAttribute('value') : '';
    }
    return this.getAttribute('value') || '';
  }
  set value(v) {
    this.valueWrites = (this.valueWrites || 0) + 1;
    this._value = String(v);
  }
  get checked() {
    return this._checked !== null ? this._checked : this.hasAttribute('checked');
  }
  set checked(v) {
    this._checked = !!v;
  }
  hasAttribute(name) {
    return this.attrs.has(name);
  }
  getAttribute(name) {
    return this.attrs.has(name) ? this.attrs.get(name) : null;
  }
  setAttribute(name, value) {
    this.attrs.set(name, String(value));
    // A browser blurs a control that becomes disabled.
    if (name === 'disabled' && this.ownerDocument._active === this) this.ownerDocument._active = null;
  }
  removeAttribute(name) {
    this.attrs.delete(name);
  }
  insertBefore(node, ref) {
    if (node.parentNode) node.parentNode.removeChild(node);
    const at = ref === null || ref === undefined ? this.childNodes.length : this.childNodes.indexOf(ref);
    assert.ok(at >= 0, 'reference node is not a child');
    this.childNodes.splice(at, 0, node);
    node.parentNode = this;
    return node;
  }
  appendChild(node) {
    return this.insertBefore(node, null);
  }
  removeChild(node) {
    const at = this.childNodes.indexOf(node);
    assert.ok(at >= 0, 'not a child');
    this.childNodes.splice(at, 1);
    node.parentNode = null;
    return node;
  }
  contains(node) {
    for (let n = node; n; n = n.parentNode) if (n === this) return true;
    return false;
  }
  descendants() {
    const out = [];
    const walk = (node) =>
      node.childNodes.forEach((child) => {
        if (child.nodeType === 1) {
          out.push(child);
          walk(child);
        }
      });
    walk(this);
    return out;
  }
  querySelectorAll(selector) {
    const chains = parseSelector(selector);
    return this.descendants().filter((n) => chains.some((chain) => matchChain(n, chain, chain.length - 1)));
  }
  querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  }
  closest(selector) {
    const chains = parseSelector(selector);
    for (let n = this; n && n.nodeType === 1; n = n.parentNode) {
      if (chains.some((chain) => matchChain(n, chain, chain.length - 1))) return n;
    }
    return null;
  }
  addEventListener(type, fn) {
    (this.listeners[type] = this.listeners[type] || []).push(fn);
  }
  removeEventListener(type, fn) {
    this.listeners[type] = (this.listeners[type] || []).filter((f) => f !== fn);
  }
  focus() {
    this.ownerDocument.focusElement(this);
  }
}

class FakeDocument {
  constructor() {
    this.listeners = {};
    this.hidden = false;
    this.body = new FakeElement(this, 'body');
    this._active = null;
  }
  createElement(tag) {
    return new FakeElement(this, tag);
  }
  createTextNode(text) {
    return new FakeText(this, String(text));
  }
  getElementById(id) {
    return this.body.descendants().find((n) => n.getAttribute('id') === id) || null;
  }
  addEventListener(type, fn) {
    (this.listeners[type] = this.listeners[type] || []).push(fn);
  }
  removeEventListener(type, fn) {
    this.listeners[type] = (this.listeners[type] || []).filter((f) => f !== fn);
  }
  get activeElement() {
    return this._active && this.body.contains(this._active) ? this._active : this.body;
  }
  focusable(el) {
    if (!this.body.contains(el)) return false;
    for (let n = el; n && n !== this.body; n = n.parentNode) {
      if (n.hasAttribute('hidden')) return false;
    }
    if (el.hasAttribute('disabled')) return false;
    if (el.hasAttribute('tabindex')) return true;
    if (el.tagName === 'A') return el.hasAttribute('href');
    if (el.tagName === 'INPUT') return el.getAttribute('type') !== 'hidden';
    return ['BUTTON', 'TEXTAREA', 'SELECT'].includes(el.tagName);
  }
  focusElement(el) {
    if (!this.focusable(el)) return;
    const previous = this.activeElement;
    if (previous === el) return;
    if (previous !== this.body) this.dispatch(previous, 'focusout', {});
    this._active = el;
  }
  dispatch(target, type, init) {
    const event = Object.assign(
      {
        type,
        target,
        defaultPrevented: false,
        preventDefault() {
          this.defaultPrevented = true;
        },
        stopPropagation() {}
      },
      init || {}
    );
    for (let n = target; n; n = n.parentNode) {
      (n.listeners[type] || []).slice().forEach((fn) => fn(event));
    }
    (this.listeners[type] || []).slice().forEach((fn) => fn(event));
    return event;
  }
}

function parseInto(doc, html, parent) {
  const re = /<!--[\s\S]*?-->|<\/([a-zA-Z0-9]+)\s*>|<([a-zA-Z0-9]+)((?:[^>"']|"[^"]*"|'[^']*')*?)(\/?)>|([^<]+)/g;
  const stack = [parent];
  let m;
  while ((m = re.exec(html)) !== null) {
    const top = stack[stack.length - 1];
    if (m[0].startsWith('<!--')) continue;
    if (m[1]) {
      const tag = m[1].toUpperCase();
      for (let i = stack.length - 1; i > 0; i--) {
        if (stack[i].tagName === tag) {
          stack.length = i;
          break;
        }
      }
    } else if (m[2]) {
      const el = doc.createElement(m[2]);
      const attr = /([^\s=>\/"']+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+)))?/g;
      let a;
      while ((a = attr.exec(m[3])) !== null) {
        const value = a[2] !== undefined ? a[2] : a[3] !== undefined ? a[3] : a[4] !== undefined ? a[4] : '';
        el.setAttribute(a[1], decode(value));
      }
      top.appendChild(el);
      if (!VOID.has(m[2].toLowerCase()) && !m[4]) stack.push(el);
    } else if (m[5] !== undefined) {
      top.appendChild(doc.createTextNode(decode(m[5])));
    }
  }
}

// ------------------------------------------------------------------ //
// time, network and the page

const T0 = Date.parse('2026-10-08T14:50:00.000Z');

function iso(offsetSeconds) {
  return new Date(T0 + offsetSeconds * 1000).toISOString();
}

function clockText(offsetSeconds) {
  return new Date(T0 + offsetSeconds * 1000).toISOString().slice(11, 19);
}

function fakeClock() {
  const clock = { current: 1_000_000, timers: [], next: 1 };
  clock.now = () => clock.current;
  clock.setTimeout = (fn, ms) => {
    const handle = clock.next++;
    clock.timers.push({ handle, at: clock.current + ms, fn });
    return handle;
  };
  clock.clearTimeout = (handle) => {
    clock.timers = clock.timers.filter((t) => t.handle !== handle);
  };
  clock.pending = () => clock.timers.length;
  clock.advance = (ms) => {
    const target = clock.current + ms;
    for (;;) {
      const due = clock.timers.filter((t) => t.at <= target).sort((a, b) => a.at - b.at || a.handle - b.handle)[0];
      if (!due) break;
      clock.timers = clock.timers.filter((t) => t !== due);
      clock.current = Math.max(clock.current, due.at);
      due.fn();
    }
    clock.current = target;
  };
  return clock;
}

const flush = () => new Promise((resolve) => setImmediate(resolve));

function response(status, body, extra) {
  const e = extra || {};
  const json = e.html === undefined;
  const text = json ? JSON.stringify(body) : body;
  return {
    status,
    redirected: !!e.redirected,
    url: e.url || '',
    headers: { get: (name) => (/content-type/i.test(name) ? (json ? 'application/json' : 'text/html; charset=utf-8') : null) },
    text: () => Promise.resolve(text)
  };
}

function fakeNetwork() {
  const net = { calls: [], concurrent: 0, peak: 0 };
  net.fetch = (url, init) =>
    new Promise((resolve, reject) => {
      const call = { url, init, method: init.method, done: false };
      const finish = () => {
        if (!call.done) {
          call.done = true;
          net.concurrent -= 1;
        }
      };
      call.respond = (status, body, extra) => {
        finish();
        resolve(response(status, body, extra));
      };
      call.html = (status, body, extra) => {
        finish();
        resolve(response(status, body, Object.assign({ html: true }, extra)));
      };
      call.fail = () => {
        finish();
        reject(new TypeError('network down'));
      };
      call.signal = init.signal;
      if (init.signal) {
        init.signal.addEventListener('abort', () => {
          finish();
          reject(Object.assign(new Error('aborted'), { name: 'AbortError' }));
        });
      }
      net.concurrent += 1;
      net.peak = Math.max(net.peak, net.concurrent);
      net.calls.push(call);
    });
  net.gets = () => net.calls.filter((c) => c.method === 'GET');
  net.posts = () => net.calls.filter((c) => c.method === 'POST');
  return net;
}

// ------------------------------------------------------------------ //
// the panel, as the server renders it (the unit tests of the Python side
// check these hooks against the real templates)

const LABELS = {
  title: 'Orga-Dashboard',
  refresh_now: 'Jetzt aktualisieren',
  refresh_retry: 'Erneut versuchen',
  fresh_ok_template: 'Automatisch alle %(seconds)d s',
  fresh_loading: 'Wird aktualisiert…',
  fresh_held: 'Automatische Aktualisierung angehalten, solange ein Formular bearbeitet wird',
  fresh_hidden: 'Angehalten, solange der Tab im Hintergrund ist',
  fresh_failed: 'Aktualisierung fehlgeschlagen – angezeigte Daten sind veraltet.',
  fresh_stopped: 'Aktualisierung gestoppt',
  fresh_ago_template: '(vor %(minutes)d min)',
  live_refreshed_template: 'Aktualisiert, Stand %(time)s',
  failure_detail_template:
    'Stand %(time)s. Stufen und Zeiten können inzwischen anders sein. Aktionen werden vom Server geprüft.',
  refresh_confirm: 'Entwurf behalten und Liste aktualisieren?',
  session_expired_heading: 'Deine Sitzung ist abgelaufen. Die Aktualisierung wurde gestoppt.',
  session_expired_detail:
    'Die Liste wurde ausgeblendet. Nach dem Anmelden kommst du zu derselben Ansicht zurück.',
  sign_in: 'Anmelden',
  access_lost_heading: 'Kein Zugriff mehr. Die Aktualisierung wurde gestoppt.',
  access_lost_detail:
    'Du kannst das Orga-Dashboard nicht mehr sehen. Wende dich bei Fragen an die Turnierleitung.',
  to_tournament_overview: 'Zur Turnierübersicht',
  draft_heading: 'Nicht gesendeter Entwurf',
  draft_detail: 'Dein Kommentar wurde nicht gesendet. Kopiere ihn bei Bedarf; er wird beim Verlassen der Seite verworfen.',
  draft_readonly: 'Entwurf (nur lesen)',
  focus_moved_template: '%(match)s ist nicht mehr fällig (bestätigt) und wurde entfernt.',
  focus_next: 'Der Fokus liegt auf der nächsten Begegnung.',
  csrf_invalid: 'Die Formularprüfung ist abgelaufen. Bitte die Seite neu laden; dein Entwurf bleibt sichtbar.',
  ack_cancel: 'Abbrechen',
  ack_counter_template: '%(count)d / %(max)d Zeichen · nur Text',
  ack_saving: 'Wird gespeichert…',
  ack_pending: 'Noch nicht bestätigt.',
  ack_success_template:
    'Prüfung festgehalten (Server-Stand %(time)s). Alarmintervall läuft neu; Wartezeit gesamt unverändert %(wait)s.',
  ack_refresh_keep: 'Jetzt aktualisieren · Entwurf behalten',
  ack_refused_template: 'Nicht festgehalten: %(reason)s',
  ack_refused_detail: 'Die Zeile zeigt jetzt den Server-Stand. Dein Entwurf bleibt zum Kopieren erhalten.',
  ack_announce: 'Prüfung festgehalten.',
  ack_failed: 'Prüfung nicht gespeichert – Verbindung fehlgeschlagen. Erneut versuchen.',
  history_show: 'Letzte Prüfungen anzeigen',
  history_hide: 'Letzte Prüfungen ausblenden',
  pin_adding: 'Wird angepinnt…',
  pin_removing: 'Wird entfernt…',
  pin_removed: 'Pin entfernt.',
  pin_failed: 'Pin nicht gespeichert – Verbindung fehlgeschlagen. Erneut versuchen.',
  tile_filtered_template: 'Gefiltert auf %(tier)s: %(count)s.',
  tile_unfiltered_template: 'Filter aufgehoben. %(count)s.'
};

const LOGIN = '/authentication/log_in';
const SITE_LIST = '/lan-tournaments/orga-dashboard';
const ADMIN_LIST = '/lan-tournaments/for_party/pixelnacht-36/dashboard';

function esc(text) {
  return String(text).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/'/g, '&#39;');
}

const TIERS = {
  r: { icon: '■', name: 'Lange Verzögerung', range: 'ab 45 min' },
  y: { icon: '▲', name: 'Verzögerung prüfen', range: '15–44 min' },
  g: { icon: '○', name: 'Unter Warnschwelle', range: 'unter 15 min' }
};
const STATE = { r: 'tier-red', y: 'tier-yellow', g: 'tier-green' };

function tile(letter, count, active, list) {
  const t = TIERS[letter];
  const inner = `<span class="ltd-sti" aria-hidden="true">${t.icon}</span><b class="ltd-stc">${count}</b><span class="ltd-stn">${t.name}</span><small>${t.range}</small>`;
  const cls = `ltd-st t-${letter}${count === 0 ? ' is-zero' : ''}${active ? ' is-on' : ''}`;
  if (count === 0) return `<li class="${cls}"><span>${inner}</span></li>`;
  const href = active ? `${list}?view=due` : `${list}?view=due&state=${STATE[letter]}`;
  const label = `${count} Begegnungen: ${t.name} (${t.range}). ${active ? 'Filter aufheben, alle Begegnungen zeigen' : 'Liste auf diese Stufe filtern'}`;
  return `<li class="${cls}"><a href="${href}" aria-label="${label}"${active ? ' aria-current="true"' : ''}>${inner}</a></li>`;
}

// One row. `n` names it (`lt-row-<n>`); options mirror the row states.
function row(n, o = {}) {
  const surface = o.surface || 'site';
  const base = surface === 'site' ? SITE_LIST : ADMIN_LIST;
  const id = `lt-row-${n}`;
  const tierKind = o.tier || 'y';
  const record = o.record
    ? `<div class="ltd-rec" id="rec-${n}" tabindex="-1"><p class="ltd-rech"><b>${esc(o.record)}</b></p>${
        o.history
          ? `<button type="button" class="ltd-disc" data-hist aria-expanded="false" aria-controls="hist-${n}">Letzte Prüfungen anzeigen <span class="ltd-dc">(2 in dieser Episode)</span></button><ol class="ltd-hist" id="hist-${n}" hidden><li><b>Mara</b> · 14:15 · aktuell</li><li><b>Jonas</b> · 14:10</li></ol>`
          : ''
      }</div>`
    : '';
  const form =
    o.ack === false
      ? ''
      : `<form class="ltd-form" id="form-${n}" method="post" action="${base}/matches/${n}/ack" data-ack${
          o.open ? ' data-open' : ''
        } aria-labelledby="fh-${n}"><input type="hidden" name="csrf_token" value="tok"><input type="hidden" name="episode" value="ep-${n}"><input type="hidden" name="revision" value="${o.revision || 1}"><input type="hidden" name="return" value="view=due"><h4 id="fh-${n}">Verzögerung geprüft – festhalten</h4><p class="ltd-help" id="help-${n}">Hilfe</p><label class="ltd-lbl" for="c-${n}">Kommentar (optional)</label><textarea id="c-${n}" name="comment" rows="3">${esc(
          o.draft || ''
        )}</textarea><p class="ltd-cnt" id="cnt-${n}">0 / 500 Zeichen · nur Text</p><div class="ltd-frow"><button type="submit" class="ltd-btn pri">Prüfung festhalten</button><button type="button" class="ltd-btn" data-ack-cancel${
          o.open ? '' : ' hidden'
        }>Abbrechen</button></div></form>`;
  const opener =
    o.ack === false
      ? '<p class="ltd-why">Pausiert – Prüfen nicht möglich.</p>'
      : `<button type="button" class="ltd-btn pri" data-ack-open aria-expanded="${o.open ? 'true' : 'false'}" aria-controls="form-${n}" hidden>Prüfung erfassen…</button>`;
  const pin = `<form class="ltd-pin" method="post" action="${base}/matches/${n}/pin"><input type="hidden" name="csrf_token" value="tok"><input type="hidden" name="revision" value="${o.pinRevision || 3}"><input type="hidden" name="return" value="view=due"><input type="hidden" name="pinned" value="${o.pinned ? 'false' : 'true'}"><button type="submit" class="ltd-btn" data-pin>${
    o.pinned ? 'Pin entfernen' : 'Für das Orga-Team anpinnen'
  }</button></form>`;
  const signal = o.pinned
    ? '<p class="ltd-sigs"><span class="ltd-sig s-pin"><i aria-hidden="true">◆</i>Angepinnt von Mara um 14:03</span></p>'
    : '';
  return `<li class="ltd-row${o.stale ? ' is-stale' : ''}${o.rowClass ? ` ${o.rowClass}` : ''}" id="${id}"><article aria-labelledby="h-${n}"><div class="ltd-tier t-${tierKind}"><span class="ltd-ti" aria-hidden="true">${
    (TIERS[tierKind] || TIERS.y).icon
  }</span><span class="ltd-tn">${o.tierName || (TIERS[tierKind] || TIERS.y).name}</span><span class="ltd-tv"><b>${
    o.wait || '20 min'
  }</b> Alarmintervall</span></div><div class="ltd-main"><p class="ltd-ctx"><a href="/lan-tournaments/${n}">${esc(
    o.tournament || 'Kupfer-Cup'
  )}</a><span>Rocket League</span><span>K.-o.</span></p><h3 id="h-${n}"><a href="/lan-tournaments/matches/${n}" class="ltd-mlink">${esc(
    o.title || `Team A${n} gegen Team B${n}`
  )}</a></h3><p class="ltd-loc">${esc(o.location || 'Gewinnerrunde · Runde 2 · Spiel 3')}</p>${signal}</div><dl class="ltd-times"><div class="is-key"><dt>Aktive Wartezeit gesamt</dt><dd>${
    o.wait || '20 min'
  }</dd></div></dl><div class="ltd-act">${opener}${pin}<span class="ltd-links"><a href="/lan-tournaments/matches/${n}">Zur Begegnung</a></span></div><div class="ltd-ack">${record}${form}</div></article></li>`;
}

function panel(o = {}) {
  const surface = o.surface || 'site';
  const list = surface === 'site' ? SITE_LIST : ADMIN_LIST;
  const offset = o.asOf === undefined ? 0 : o.asOf;
  const rows = (o.rows || [row(1), row(2), row(3)]).join('');
  const tiles = o.tiles || { r: 0, y: 3, g: 0, active: null };
  const scope =
    surface === 'admin'
      ? `<form class="ltd-scope" method="get" action="${list}"><fieldset><legend class="ltd-k">Umfang</legend><label><input type="radio" name="scope" value="assigned" checked> Zugewiesene Turniere</label><label><input type="radio" name="scope" value="all"> Alle Turniere dieser Party</label></fieldset><button class="ltd-btn" type="submit">Umfang anwenden</button></form>`
      : `<p class="ltd-scope is-fixed"><span class="ltd-k">Umfang</span> <b>Zugewiesene Turniere</b></p>`;
  const query = o.query !== undefined ? o.query : '?view=due&sort=urgency';
  const attrs = [
    'class="lt-dashboard' + (surface === 'site' ? ' is-site' : '') + '"',
    'data-lt-dashboard-root',
    `data-surface="${surface}"`,
    `data-poll-url="${list}/poll${query}"`,
    `data-poll-seconds="${o.pollSeconds || 30}"`,
    `data-as-of="${iso(offset)}"`,
    `data-login-url="${LOGIN}"`,
    o.overviewUrl ? `data-overview-url="${o.overviewUrl}"` : '',
    `data-labels='${esc(JSON.stringify(o.labels || LABELS))}'`,
    'aria-label="Orga-Dashboard"'
  ]
    .filter(Boolean)
    .join(' ');
  const count = o.count || `${(o.rows || [1, 2, 3]).length} Begegnungen`;
  return (
    `<section ${attrs}>` +
    `<div class="ltd-top">${scope}<div class="ltd-fresh"><p><span class="ltd-k">Stand</span> <b>${
      o.time || clockText(offset)
    }</b> <span class="ltd-tz">MESZ</span></p><p class="ltd-fst" aria-live="polite"><span class="ltd-fst-auto">Automatisch alle 30 s</span><span class="ltd-fst-manual">Ohne JavaScript: nur manuelle Aktualisierung</span></p>` +
    `<form class="ltd-refresh" method="get" action="${list}"><input type="hidden" name="view" value="due"><input type="hidden" name="state" value="all"><input type="hidden" name="sort" value="urgency"><input type="hidden" name="page" value="1"><button class="ltd-btn" type="submit" data-refresh>Jetzt aktualisieren</button></form></div></div>` +
    `<section class="ltd-sum"><h2 class="ltd-k">Aktuell fällig nach Stufe</h2><ul>${tile('r', tiles.r, tiles.active === 'r', list)}${tile(
      'y',
      tiles.y,
      tiles.active === 'y',
      list
    )}${tile('g', tiles.g, tiles.active === 'g', list)}</ul></section>` +
    `<form class="ltd-filters" method="get" action="${list}" role="search"><nav class="ltd-views"><a href="${list}?view=due" aria-current="page">Aktuell fällig</a></nav><div class="ltd-fl"><div class="ltd-fld"><label class="ltd-lbl" for="lt-filter-sort">Sortierung</label><select id="lt-filter-sort" name="sort"><option value="urgency" selected>Dringlichkeit</option><option value="wait">Aktive Wartezeit gesamt</option></select></div><button class="ltd-btn pri" type="submit">Anwenden</button></div>${
      o.chips
        ? `<p class="ltd-applied"><span class="ltd-k">Angewendet</span>${o.chips.map((c) => `<span class="ltd-chip">${esc(c)}</span>`).join('')}</p>`
        : ''
    }</form>` +
    (o.banner
      ? `<div class="ltd-ban b-info" role="status"><p class="ltd-banh"><i aria-hidden="true">i</i>${esc(o.banner)}</p></div>`
      : '') +
    `<div class="ltd-count"><h2 id="ltd-count">${count}</h2><p>Aktuell fällig</p></div>` +
    `<ol class="ltd-rows" aria-labelledby="ltd-count">${rows}</ol>` +
    (o.pager
      ? `<nav class="ltd-pager" aria-label="Seiten">${o.pager.map((n) => `<a class="ltd-btn" href="${list}?view=due&page=${n}">${n}</a>`).join('')}</nav>`
      : '') +
    `<div class="ltd-sr" aria-live="polite" data-live></div></section>`
  );
}

// ------------------------------------------------------------------ //
// the harness

function setup(o = {}) {
  const doc = new FakeDocument();
  const clock = fakeClock();
  const net = fakeNetwork();
  const html = o.html || panel(o.panel || {});
  parseInto(doc, html, doc.body);
  const root = doc.body.querySelector('[data-lt-dashboard-root]');
  const location = {
    href: `https://lan.example${o.path || SITE_LIST}${o.search || '?view=due&sort=urgency'}`,
    pathname: o.path || SITE_LIST,
    search: o.search || '?view=due&sort=urgency',
    assigned: [],
    reloads: 0,
    assign(url) {
      this.assigned.push(url);
    },
    reload() {
      this.reloads += 1;
    }
  };
  const history = {
    pushes: [],
    pushState(state, title, url) {
      this.pushes.push(url);
    }
  };
  const win = { listeners: {}, addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); } };
  const parseFragment = (text) => {
    const holder = doc.createElement('div');
    parseInto(doc, text, holder);
    return holder.querySelector('[data-lt-dashboard-root]');
  };
  const options = Object.assign(
    {
      document: doc,
      window: win,
      fetch: net.fetch,
      setTimeout: clock.setTimeout,
      clearTimeout: clock.clearTimeout,
      now: clock.now,
      isHidden: () => doc.hidden,
      location,
      history,
      parseFragment
    },
    o.options || {}
  );
  const controller = initDashboard(root, options);
  const t = { doc, root, clock, net, location, history, win, controller, options, parseFragment };
  t.q = (selector) => root.querySelector(selector);
  t.qa = (selector) => root.querySelectorAll(selector);
  t.text = (selector) => {
    const el = root.querySelector(selector);
    return el ? el.textContent.trim() : null;
  };
  t.fragment = (p) => ({ html: panel(p), as_of: iso(p && p.asOf !== undefined ? p.asOf : 0), poll_seconds: (p && p.pollSeconds) || 30 });
  // The default of a click: a submit button submits its form, a link
  // navigates, unless a handler said no.
  t.click = (el, init) => {
    el.focus();
    const event = doc.dispatch(el, 'click', init);
    if (event.defaultPrevented) return event;
    const form = el.closest('form');
    if (form && el.tagName === 'BUTTON' && (el.getAttribute('type') || 'submit') === 'submit' && !el.hasAttribute('disabled')) {
      const submit = doc.dispatch(form, 'submit', {});
      if (!submit.defaultPrevented) (t.nativeSubmits = t.nativeSubmits || []).push(form);
    } else if (el.closest('a[href]')) {
      (t.navigations = t.navigations || []).push(el.closest('a[href]').getAttribute('href'));
    }
    return event;
  };
  t.type = (area, text) => {
    area.value = text;
    doc.dispatch(area, 'input', {});
  };
  t.visibility = (hidden) => {
    doc.hidden = hidden;
    doc.dispatch(doc.body, 'visibilitychange', {});
  };
  t.tick = async (ms) => {
    clock.advance(ms);
    await flush();
  };
  t.open = (n) => {
    const opener = root.querySelector(`#lt-row-${n} button[data-ack-open]`);
    t.click(opener);
    return root.querySelector(`#form-${n}`);
  };
  // Answer the open request with a panel.
  t.answer = async (call, p) => {
    call.respond(200, t.fragment(p));
    await flush();
  };
  return t;
}

const rowIds = (t) => t.qa('li.ltd-row').map((n) => n.id);

// ------------------------------------------------------------------ //
// the named behaviours

test('test_no_overlapping_or_out_of_order_polls', async () => {
  const t = setup();
  assert.equal(t.net.calls.length, 0, 'no request before the first interval');
  await t.tick(29999);
  assert.equal(t.net.calls.length, 0);
  await t.tick(1);
  assert.equal(t.net.calls.length, 1);
  const first = t.net.calls[0];
  assert.equal(first.method, 'GET');
  assert.equal(first.url, `${SITE_LIST}/poll?view=due&sort=urgency`);
  assert.equal(first.init.headers.Accept, 'application/json');
  assert.equal(first.init.credentials, 'same-origin');
  assert.equal(first.init.body, undefined);

  // Nothing overlaps: a manual refresh, a tick and a retry join the request.
  t.click(t.q(HOOKS.refreshButton));
  await t.tick(10000);
  t.controller.refresh();
  await flush();
  assert.equal(t.net.calls.length, 1);
  assert.equal(t.net.peak, 1);

  // A newer snapshot replaces the panel and moves the shown time.
  await t.answer(first, { asOf: 60, rows: [row(1, { tierName: 'NEU' }), row(2)] });
  assert.equal(t.root.getAttribute('data-as-of'), iso(60));
  assert.equal(t.text('#lt-row-1 .ltd-tn'), 'NEU');

  // An older snapshot is discarded; the newer one stays on screen.
  await t.tick(30000);
  assert.equal(t.net.calls.length, 2);
  await t.answer(t.net.calls[1], { asOf: 30, rows: [row(1, { tierName: 'ALT' })] });
  assert.equal(t.root.getAttribute('data-as-of'), iso(60));
  assert.equal(t.text('#lt-row-1 .ltd-tn'), 'NEU');
  assert.equal(t.root.querySelectorAll('li.ltd-row').length, 2);
  assert.equal(t.controller.state().failed, false, 'an old answer is not a failure');

  // An answer for a superseded request is ignored, even a newer one.
  await t.tick(30000);
  const stale = t.net.calls[2];
  const tileLink = t.q('li.ltd-st.t-y a');
  t.click(tileLink);
  await flush();
  assert.equal(stale.signal.aborted, true, 'the new request drops the running one');
  assert.equal(t.net.calls.length, 4);
  stale.respond(200, t.fragment({ asOf: 500, rows: [row(9)] }));
  await flush();
  assert.ok(!rowIds(t).includes('lt-row-9'));
  assert.equal(t.net.peak, 1, 'the dropped request ends before the new one starts');
  assert.ok(t.net.gets().length === t.net.calls.length, 'a poll never posts');
});

test('test_edit_submit_and_focus_are_preserved', async () => {
  const t = setup({
    panel: { rows: [row(1, { record: 'Zuletzt geprüft von Mara um 14:15', history: true }), row(2)] },
    options: { timeoutMs: 3_600_000 }
  });
  const form = t.open(1);
  const area = form.querySelector('textarea');
  assert.equal(form.hasAttribute('data-open'), true);
  assert.equal(t.doc.activeElement, area, 'opening moves the focus to the field');
  assert.equal(t.q('#lt-row-1 button[data-ack-open]').getAttribute('aria-expanded'), 'true');
  assert.equal(t.q('#form-1 button[data-ack-cancel]').hasAttribute('hidden'), false);
  t.type(area, 'Beide Captains kontaktiert');
  assert.equal(t.text('#cnt-1'), '26 / 500 Zeichen · nur Text');

  // While the form is open nothing is fetched and nothing is replaced.
  await t.tick(300000);
  assert.equal(t.net.calls.length, 0);
  assert.equal(t.doc.getElementById('c-1'), area, 'the field is the same node');
  assert.equal(area.value, 'Beide Captains kontaktiert');
  assert.equal(t.doc.activeElement, area);
  assert.equal(t.text('.ltd-fst .ltd-fst-auto'), LABELS.fresh_held);
  assert.ok(t.q('.ltd-fresh').classList.contains('is-held'));

  // Submitting is pending: no success is claimed, the field is read only.
  t.click(t.q('#form-1 button[type="submit"]'));
  await flush();
  assert.equal(t.net.posts().length, 1);
  const post = t.net.posts()[0];
  assert.equal(post.url, `${SITE_LIST}/matches/1/ack`);
  assert.equal(post.init.headers.Accept, 'application/json');
  assert.match(post.init.body, /(^|&)comment=Beide\+Captains\+kontaktiert(&|$)/);
  assert.match(post.init.body, /(^|&)episode=ep-1(&|$)/);
  assert.match(post.init.body, /(^|&)revision=1(&|$)/);
  assert.match(post.init.body, /(^|&)csrf_token=tok(&|$)/);
  assert.equal(area.hasAttribute('readonly'), true);
  const submit = t.q('#form-1 button[type="submit"]');
  assert.equal(submit.hasAttribute('disabled'), true);
  assert.equal(submit.textContent, LABELS.ack_saving);
  assert.equal(t.text('#form-1 .ltd-pend'), LABELS.ack_pending);
  assert.equal(t.q('.ltd-okmsg'), null, 'nothing is shown as saved yet');
  assert.equal(t.q('#form-1 button[data-ack-cancel]').hasAttribute('disabled'), true);
  // A second submit is ignored while one runs.
  t.doc.dispatch(t.q('#form-1'), 'submit', {});
  assert.equal(t.net.posts().length, 1);
  await t.tick(300000);
  assert.equal(t.net.gets().length, 0, 'no poll while submitting');

  // The server confirms: the panel is replaced, the record is the new
  // latest one, the success line carries the server's stand and wait.
  post.respond(200, {
    committed_at: iso(140),
    fragment: t.fragment({
      asOf: 140,
      rows: [row(1, { record: 'Zuletzt geprüft von Jonas um 14:52', history: true, tier: 'g', wait: '15 min' }), row(2)]
    })
  });
  await flush();
  assert.equal(t.q('#form-1').hasAttribute('data-open'), false);
  assert.equal(t.text('#lt-row-1 .ltd-okmsg'), 'Prüfung festgehalten (Server-Stand 14:52:20). Alarmintervall läuft neu; Wartezeit gesamt unverändert 15 min.');
  assert.equal(t.doc.activeElement, t.q('#rec-1'), 'the focus moves to the new record');
  assert.equal(t.text('[data-live]'), LABELS.ack_announce);
  assert.equal(t.controller.state().hold, null);
  await t.tick(30000);
  assert.equal(t.net.gets().length, 1, 'polling resumes after the save');
});

test('test_transient_failure_marks_stale', async () => {
  const t = setup();
  await t.tick(30000);
  t.net.calls[0].fail();
  await flush();
  // The last data stays, marked stale, with the retry offered.
  assert.equal(rowIds(t).length, 3);
  assert.ok(t.root.classList.contains('is-stale'));
  assert.ok(t.q('li.ltd-row').classList.contains('is-stale'));
  assert.ok(t.q('.ltd-fresh').classList.contains('is-fail'));
  assert.equal(t.text('.ltd-fst .ltd-fst-auto'), LABELS.fresh_failed);
  const banner = t.q('.ltd-ban.b-err');
  assert.equal(banner.getAttribute('role'), 'alert');
  assert.equal(t.text('.ltd-ban .ltd-banh'), `!${LABELS.fresh_failed}`);
  assert.match(t.text('.ltd-ban .ltd-banb'), /^Stand 14:50:00\. Stufen und Zeiten/);
  assert.equal(t.text('.ltd-refresh button'), LABELS.refresh_retry);
  assert.ok(t.q('.ltd-refresh button').classList.contains('pri'));
  assert.equal(t.q('.ltd-ago'), null, 'under a minute: no suffix');
  assert.equal(t.controller.state().stopped, false);

  // Polling goes on, and the suffix follows the age of the data.
  await t.tick(30000);
  assert.equal(t.net.calls.length, 2);
  t.net.calls[1].html(503, '<html>bad gateway</html>');
  await flush();
  assert.equal(t.text('.ltd-ago'), '(vor 1 min)');
  assert.equal(t.root.querySelectorAll('.ltd-ban').length, 1, 'one notice, not one per failure');
  await t.tick(30000);
  t.net.calls[2].fail();
  await flush();
  await t.tick(30000);
  t.net.calls[3].fail();
  await flush();
  assert.equal(t.text('.ltd-ago'), '(vor 2 min)');

  // A timeout is the same outage.
  await t.tick(30000);
  assert.equal(t.net.calls.length, 5);
  await t.tick(15000);
  assert.equal(t.net.calls[4].signal.aborted, true);
  await flush();
  assert.equal(t.controller.state().failed, true);
  assert.equal(t.controller.state().stopped, false);

  // The retry button asks again; success clears every mark and says so.
  t.click(t.q('.ltd-ban button'));
  await flush();
  const retry = t.net.calls[t.net.calls.length - 1];
  assert.equal(retry.method, 'GET');
  await t.answer(retry, { asOf: 200, rows: [row(1), row(2), row(3)] });
  assert.equal(t.root.classList.contains('is-stale'), false);
  assert.equal(t.q('.ltd-ban'), null);
  assert.equal(t.q('.ltd-ago'), null);
  assert.equal(t.text('.ltd-refresh button'), LABELS.refresh_now);
  assert.equal(t.text('[data-live]'), 'Aktualisiert, Stand 14:53:20');
  assert.equal(t.controller.state().failed, false);
});

test('test_permission_loss_clears_and_stops', async () => {
  // 403 on the poll: rows, tiles, filters and the refresh leave the DOM.
  const t = setup();
  await t.tick(30000);
  const open = t.net.calls.length;
  t.net.calls[0].respond(403, { error: 'access_revoked', message: 'x' });
  await flush();
  for (const selector of ['ol.ltd-rows', 'li.ltd-row', '.ltd-sum', 'form.ltd-filters', '.ltd-count', 'form[data-ack]', 'form.ltd-pin', 'form.ltd-refresh', '.ltd-scope']) {
    assert.equal(t.q(selector), null, selector);
  }
  assert.equal(t.text('.ltd-ban .ltd-banh'), `!${LABELS.access_lost_heading}`);
  assert.equal(t.text('.ltd-ban .ltd-banb'), LABELS.access_lost_detail);
  const link = t.q('.ltd-ban a');
  assert.equal(link.textContent, LABELS.to_tournament_overview);
  assert.equal(link.getAttribute('href'), '/lan-tournaments/');
  assert.equal(t.q('.ltd-sign'), null);
  assert.equal(t.text('.ltd-fst .ltd-fst-auto'), LABELS.fresh_stopped);
  assert.ok(t.q('.ltd-fresh').classList.contains('is-fail'));
  assert.equal(t.doc.activeElement, t.q('.ltd-ban'), 'the focus does not fall to the page');
  assert.equal(t.controller.state().stopped, true);
  assert.equal(t.clock.pending(), 0, 'no timer is left');
  await t.tick(3600000);
  assert.equal(t.net.calls.length, open, 'no request after the loss');
  assert.equal(t.net.calls.every((c) => c.method === 'GET'), true);

  // An explicit overview URL beats the derived one; the admin derives its own.
  const admin = setup({ panel: { surface: 'admin' }, path: ADMIN_LIST });
  await admin.tick(30000);
  admin.net.calls[0].respond(403, {});
  await flush();
  assert.equal(admin.q('.ltd-ban a').getAttribute('href'), '/lan-tournaments/for_party/pixelnacht-36');
  const given = setup({ panel: { overviewUrl: '/lan-tournaments/elsewhere' } });
  await given.tick(30000);
  given.net.calls[0].respond(403, {});
  await flush();
  assert.equal(given.q('.ltd-ban a').getAttribute('href'), '/lan-tournaments/elsewhere');

  // 401: sign in again with the way back; the draft stays, read only.
  const lost = setup({ panel: { rows: [row(1, { record: 'Zuletzt geprüft von Mara um 14:15' }), row(2)] } });
  const form = lost.open(1);
  lost.type(form.querySelector('textarea'), 'Nori hängt noch im Kupfer-Cup <b>fett</b>');
  lost.click(lost.q('.ltd-refresh button'));
  await flush();
  assert.equal(lost.net.calls.length, 0, 'the question comes first');
  lost.click(lost.q('[data-ltd-js-yes]'));
  await flush();
  lost.net.calls[0].respond(401, { error: 'session_expired', message: 'x' });
  await flush();
  assert.equal(lost.q('form'), null, 'not one form is left to send it again');
  assert.equal(lost.q('li.ltd-row'), null);
  assert.equal(lost.text('.ltd-ban .ltd-banh'), `!${LABELS.session_expired_heading}`);
  const signIn = lost.q('.ltd-ban a');
  assert.equal(signIn.textContent, LABELS.sign_in);
  assert.equal(signIn.getAttribute('href'), `${LOGIN}?next=${encodeURIComponent(`${SITE_LIST}?view=due&sort=urgency`)}`);
  assert.equal(lost.text('.ltd-draft h2'), LABELS.draft_heading);
  const card = lost.q('.ltd-draft textarea');
  assert.equal(card.hasAttribute('readonly'), true);
  assert.equal(card.value, 'Nori hängt noch im Kupfer-Cup <b>fett</b>');
  assert.equal(lost.q('.ltd-draft b'), null, 'the draft is text');
  const sent = lost.net.calls.length;
  await lost.tick(3600000);
  lost.doc.dispatch(lost.root, 'submit', {});
  assert.equal(lost.net.calls.length, sent, 'neither a retry nor the draft is sent');
  assert.equal(lost.net.posts().length, 0);
  assert.equal(lost.clock.pending(), 0);

  // The same through a rejected submit: 403 `access_revoked`.
  const post = setup();
  const pf = post.open(1);
  post.type(pf.querySelector('textarea'), 'Entwurf');
  post.click(post.q('#form-1 button[type="submit"]'));
  await flush();
  post.net.posts()[0].respond(403, { error: 'access_revoked', message: 'x' });
  await flush();
  assert.equal(post.q('li.ltd-row'), null);
  assert.equal(post.q('.ltd-draft textarea').value, 'Entwurf');
  assert.equal(post.net.posts().length, 1);
});

test('test_native_refresh_needs_no_js', async () => {
  // Without a fetch the script leaves the native markup alone.
  const plain = setup({ options: { fetch: null } });
  assert.equal(plain.controller, null);
  assert.equal(plain.root.classList.contains('is-js'), false);
  assert.equal(plain.q('button[data-ack-open]').hasAttribute('hidden'), true);
  assert.equal(plain.q('form.ltd-refresh').getAttribute('method'), 'get');
  assert.equal(plain.clock.pending(), 0);

  // With it, the refresh is still a plain GET form that the script only
  // takes over, and gives back when it stops.
  const t = setup();
  const form = t.q('form.ltd-refresh');
  const before = JSON.stringify(form.attributes);
  const fields = t.qa('form.ltd-refresh input').map((i) => `${i.getAttribute('name')}=${i.getAttribute('value')}`);
  assert.equal(form.getAttribute('method'), 'get');
  assert.equal(form.getAttribute('action'), SITE_LIST);
  assert.deepEqual(fields, ['view=due', 'state=all', 'sort=urgency', 'page=1']);
  assert.equal(JSON.stringify(form.attributes), before, 'the script left the form as it was');
  const button = t.q('form.ltd-refresh button');
  assert.equal(button.getAttribute('type'), 'submit');
  t.controller.stop();
  const event = t.click(button);
  assert.equal(event.defaultPrevented, false, 'after the script stops the browser does the GET');
  assert.equal(t.nativeSubmits.length, 1);
  assert.equal(t.net.calls.length, 0);

  // The script adds its marks and reveals only what it can serve.
  const live = setup();
  assert.equal(live.root.classList.contains('is-js'), true);
  assert.equal(live.q('button[data-ack-open]').hasAttribute('hidden'), false);
  assert.equal(live.q('#form-1 button[data-ack-cancel]').hasAttribute('hidden'), true);
  assert.equal(live.text('.ltd-fst-auto'), 'Automatisch alle 30 s');
  assert.ok(live.q('.ltd-fst-manual'), 'the manual text stays in the markup for the style sheet');
  // Every other native control stays native: tiles and filters are links and a GET form.
  live.click(live.q('form.ltd-filters button'), {});
  assert.equal(live.nativeSubmits.length, 1);
  live.click(live.q('.ltd-views a'), { ctrlKey: false });
  assert.equal(live.navigations[0], `${SITE_LIST}?view=due`);

  // A native refusal re-renders the form open: the script keeps it open and holds.
  const refusal = setup({ panel: { rows: [row(1, { open: true, draft: 'Entwurf' }), row(2)] } });
  assert.equal(refusal.q('#form-1').hasAttribute('data-open'), true);
  assert.equal(refusal.q('#form-1 button[data-ack-cancel]').hasAttribute('hidden'), false);
  assert.equal(refusal.q('#form-1 textarea').value, 'Entwurf');
  assert.equal(refusal.controller.state().hold, 'editing');
  await refusal.tick(600000);
  assert.equal(refusal.net.calls.length, 0);
});

test('test_redirected_or_non_json_poll_is_session_loss', async () => {
  const cases = [
    ['redirect to the login form', (c) => c.html(200, '<form>login</form>', { redirected: true, url: `https://lan.example${LOGIN}` })],
    ['redirect to the login form with its way back', (c) => c.html(200, '<form>login</form>', { redirected: true, url: `https://lan.example${LOGIN}?next=%2Fx` })],
    ['401 with an HTML body', (c) => c.html(401, '<h1>Unauthorized</h1>')],
    ['401 JSON', (c) => c.respond(401, { error: 'session_expired', message: 'x' })],
    ['the code without the status', (c) => c.respond(200, { error: 'session_expired' })]
  ];
  for (const [name, answer] of cases) {
    const t = setup();
    await t.tick(30000);
    answer(t.net.calls[0]);
    await flush();
    assert.equal(t.q('li.ltd-row'), null, name);
    assert.equal(t.text('.ltd-ban .ltd-banh'), `!${LABELS.session_expired_heading}`, name);
    assert.ok(t.q('.ltd-ban a').getAttribute('href').startsWith(`${LOGIN}?next=`), name);
    assert.equal(t.controller.state().stopped, true, name);
    assert.equal(t.clock.pending(), 0, name);
  }

  // Not the login form: the page keeps its data and tries again.
  const others = [
    ['redirect somewhere else', (c) => c.html(200, '<p>moved</p>', { redirected: true, url: 'https://lan.example/lan-tournaments/moved' })],
    ['a path that only starts like the login form', (c) => c.html(200, '<p>help</p>', { redirected: true, url: `https://lan.example${LOGIN}-help` })],
    ['the login path without a redirect', (c) => c.html(200, '<p>odd</p>', { redirected: false, url: `https://lan.example${LOGIN}` })]
  ];
  for (const [name, answer] of others) {
    const t = setup();
    await t.tick(30000);
    answer(t.net.calls[0]);
    await flush();
    assert.equal(t.qa('li.ltd-row').length, 3, name);
    assert.equal(t.controller.state().stopped, false, name);
    assert.equal(t.controller.state().failed, true, name);
  }
});

test('test_focus_fallback_and_hold_reasons', async () => {
  // Hidden tab: no request, the reason is stated; back: one request at once.
  const t = setup();
  t.visibility(true);
  assert.equal(t.text('.ltd-fst .ltd-fst-auto'), LABELS.fresh_hidden);
  assert.ok(t.q('.ltd-fresh').classList.contains('is-held'));
  await t.tick(600000);
  assert.equal(t.net.calls.length, 0);
  assert.equal(t.text('.ltd-ago'), '(vor 10 min)');
  t.visibility(false);
  await flush();
  assert.equal(t.net.calls.length, 1, 'one fetch on resume');
  assert.equal(t.net.calls[0].method, 'GET');
  await t.answer(t.net.calls[0], { asOf: 600 });
  assert.equal(t.text('.ltd-fst .ltd-fst-auto'), LABELS.fresh_ok_template.replace('%(seconds)d', '30'));
  assert.equal(t.q('.ltd-ago'), null);
  await t.tick(30000);
  assert.equal(t.net.calls.length, 2, 'the cadence is back');

  // A form holds with its own reason; closing it fetches once.
  const e = setup();
  const form = e.open(1);
  assert.equal(e.text('.ltd-fst .ltd-fst-auto'), LABELS.fresh_held);
  e.type(form.querySelector('textarea'), 'abc');
  e.visibility(true);
  assert.equal(e.text('.ltd-fst .ltd-fst-auto'), LABELS.fresh_held, 'the form is the stronger reason');
  e.visibility(false);
  await flush();
  assert.equal(e.net.calls.length, 0, 'the form still holds');
  e.click(e.q('#form-1 button[data-ack-cancel]'));
  await flush();
  assert.equal(e.net.calls.length, 1);
  assert.equal(e.doc.activeElement, e.q('#lt-row-1 button[data-ack-open]'), 'cancel returns to the opener');
  assert.equal(e.q('#form-1 textarea').value, '', 'a cancelled draft is gone');
  assert.equal(e.q('#form-1').hasAttribute('data-open'), false);
  await e.answer(e.net.calls[0], { asOf: 5 });
  assert.equal(e.text('.ltd-fst .ltd-fst-auto'), LABELS.fresh_ok_template.replace('%(seconds)d', '30'));

  // A poll that was in flight when the form opened must not replace it.
  const r = setup();
  await r.tick(30000);
  const running = r.net.calls[0];
  const open = r.open(2);
  r.type(open.querySelector('textarea'), 'noch nicht fertig');
  await r.answer(running, { asOf: 30, rows: [row(1, { tierName: 'X' })] });
  assert.equal(r.doc.getElementById('form-2'), open, 'the open form is the same node');
  assert.equal(r.q('#c-2').value, 'noch nicht fertig');
  assert.equal(r.controller.state().pendingResume, true);
  r.click(r.q('#form-2 button[data-ack-cancel]'));
  await flush();
  assert.equal(r.net.calls.length, 2, 'fetch once on resume');

  // The focused row leaves: the next row's heading, else the previous, else the count.
  async function leave(rowsBefore, rowsAfter, focusN) {
    const s = setup({ panel: { rows: rowsBefore.map((n) => row(n, { tournament: 'Kupfer-Cup', location: `Spiel ${n}` })) } });
    s.q(`#lt-row-${focusN} .ltd-mlink`).focus();
    assert.equal(s.doc.activeElement, s.q(`#lt-row-${focusN} .ltd-mlink`));
    await s.tick(30000);
    await s.answer(s.net.calls[0], { asOf: 30, rows: rowsAfter.map((n) => row(n)) });
    return s;
  }
  const next = await leave([1, 2, 3], [1, 3], 2);
  assert.equal(next.doc.activeElement, next.q('#lt-row-3 h3'));
  assert.ok(next.q('#lt-row-3').classList.contains('is-focus'));
  assert.equal(next.text('.ltd-ban.b-info .ltd-banh'), `iKupfer-Cup · Spiel 2 ist nicht mehr fällig (bestätigt) und wurde entfernt.`);
  assert.equal(next.text('.ltd-ban.b-info .ltd-banb'), LABELS.focus_next);
  assert.equal(next.q('.ltd-ban.b-info').getAttribute('role'), 'status');
  next.doc.dispatch(next.doc.activeElement, 'focusout', {});
  assert.equal(next.q('#lt-row-3').classList.contains('is-focus'), false, 'the mark ends with the focus');

  const previous = await leave([1, 2, 3], [1, 2], 3);
  assert.equal(previous.doc.activeElement, previous.q('#lt-row-2 h3'));
  assert.equal(previous.q('.ltd-ban.b-info .ltd-banb'), null, 'only the next row is announced as such');

  const none = await leave([1, 2], [], 1);
  assert.equal(none.doc.activeElement, none.q('#ltd-count'));
  assert.equal(none.q('#ltd-count').getAttribute('tabindex'), '-1');

  // A row that stays keeps the focus on the same control.
  const same = await leave([1, 2, 3], [1, 2, 3], 2);
  assert.equal(same.doc.activeElement, same.q('#lt-row-2 .ltd-mlink'));
  assert.equal(same.q('.ltd-ban.b-info'), null);
  const pin = setup();
  pin.q('#lt-row-2 button[data-pin]').focus();
  await pin.tick(30000);
  await pin.answer(pin.net.calls[0], { asOf: 30 });
  assert.equal(pin.doc.activeElement, pin.q('#lt-row-2 button[data-pin]'));
});

test('test_tile_toggle_keeps_focus_and_announces', async () => {
  const t = setup();
  const link = t.q('li.ltd-st.t-y a');
  link.focus();
  assert.equal(t.doc.activeElement, link);
  const event = t.click(link);
  assert.equal(event.defaultPrevented, true);
  await flush();
  assert.equal(t.net.calls.length, 1);
  assert.equal(t.net.calls[0].url, `${SITE_LIST}/poll?view=due&state=tier-yellow`, 'the poll of the tile\'s query, page dropped');
  await t.answer(t.net.calls[0], {
    asOf: 5,
    tiles: { r: 0, y: 3, g: 0, active: 'y' },
    query: '?view=due&state=tier-yellow&sort=urgency',
    count: '3 Begegnungen'
  });
  const active = t.q('li.ltd-st.t-y a');
  assert.notEqual(active, link, 'the panel was replaced');
  assert.equal(t.doc.activeElement, active, 'the focus is on the same tile');
  assert.equal(active.getAttribute('aria-current'), 'true');
  assert.equal(t.text('[data-live]'), 'Gefiltert auf Verzögerung prüfen: 3 Begegnungen.');
  assert.deepEqual(t.history.pushes, [`${SITE_LIST}?view=due&state=tier-yellow`]);
  assert.equal(t.root.getAttribute('data-poll-url'), `${SITE_LIST}/poll?view=due&state=tier-yellow&sort=urgency`, 'later polls keep the filter');

  // The active tile toggles the filter off again.
  t.click(active);
  await flush();
  assert.equal(t.net.calls[1].url, `${SITE_LIST}/poll?view=due`);
  await t.answer(t.net.calls[1], { asOf: 9, rows: [row(1), row(2), row(3), row(4)], count: '4 Begegnungen' });
  assert.equal(t.doc.activeElement, t.q('li.ltd-st.t-y a'));
  assert.equal(t.q('li.ltd-st.t-y a').getAttribute('aria-current'), null);
  assert.equal(t.text('[data-live]'), 'Filter aufgehoben. 4 Begegnungen.');
  assert.equal(t.history.pushes.length, 2);

  // A zero tile is not a link; a modified click, another button, an open form and a stopped page stay native.
  const zero = t.q('li.ltd-st.t-r');
  assert.equal(zero.querySelector('a'), null);
  const calls = t.net.calls.length;
  for (const init of [{ ctrlKey: true }, { metaKey: true }, { shiftKey: true }, { button: 1 }]) {
    const e = t.click(t.q('li.ltd-st.t-y a'), init);
    assert.equal(e.defaultPrevented, false, JSON.stringify(init));
  }
  assert.equal(t.net.calls.length, calls);
  t.open(1);
  const withForm = t.click(t.q('li.ltd-st.t-y a'));
  assert.equal(withForm.defaultPrevented, false, 'a draft is never replaced by a link');
  assert.equal(t.net.calls.length, calls);

  // Without the labels the tile's own label is announced.
  const bare = setup({ panel: { labels: Object.assign({}, LABELS, { tile_filtered_template: undefined, tile_unfiltered_template: undefined }) } });
  bare.click(bare.q('li.ltd-st.t-y a'));
  await flush();
  await bare.answer(bare.net.calls[0], { asOf: 5, tiles: { r: 0, y: 3, g: 0, active: 'y' }, labels: Object.assign({}, LABELS, { tile_filtered_template: undefined }) });
  assert.match(bare.text('[data-live]'), /^3 Begegnungen: Verzögerung prüfen/);

  // A failed request falls back to the plain navigation.
  const down = setup();
  down.click(down.q('li.ltd-st.t-y a'));
  await flush();
  down.net.calls[0].fail();
  await flush();
  assert.deepEqual(down.location.assigned, [`${SITE_LIST}?view=due&state=tier-yellow`]);
  assert.equal(down.controller.state().failed, false);

  // Back navigation reloads the page for the URL it returns to.
  t.win.listeners.popstate.forEach((fn) => fn({}));
  assert.equal(t.location.reloads, 1);
  const fresh = setup();
  fresh.win.listeners.popstate.forEach((fn) => fn({}));
  assert.equal(fresh.location.reloads, 0, 'nothing happened on this page yet');
});

test('test_refusal_replaces_panel_and_keeps_draft', async () => {
  // 409 stale: the whole panel comes back, the draft goes into the reopened form.
  const t = setup({ panel: { rows: [row(1), row(2)], tiles: { r: 0, y: 2, g: 0, active: null } } });
  const form = t.open(1);
  t.type(form.querySelector('textarea'), 'Nachtbus-Spieler gefunden, sitzt an C-14.');
  t.click(t.q('#form-1 button[type="submit"]'));
  await flush();
  t.net.posts()[0].respond(409, {
    error: 'stale',
    message: 'Der Stand hat sich geändert. Bitte aktualisieren und erneut prüfen.',
    draft_target: true,
    fragment: t.fragment({
      asOf: 77,
      rows: [row(1, { record: 'Zuletzt geprüft von Mara um 14:51', revision: 4, tier: 'r', tierName: 'Lange Verzögerung' }), row(2)],
      tiles: { r: 1, y: 1, g: 0, active: null }
    })
  });
  await flush();
  assert.equal(t.root.getAttribute('data-as-of'), iso(77), 'freshness advanced with the panel');
  assert.equal(t.text('li.ltd-st.t-r .ltd-stc'), '1', 'tiles come from the same snapshot');
  assert.equal(t.text('#lt-row-1 .ltd-tn'), 'Lange Verzögerung');
  const reopened = t.q('#form-1');
  assert.equal(reopened.hasAttribute('data-open'), true);
  assert.equal(reopened.querySelector('textarea').value, 'Nachtbus-Spieler gefunden, sitzt an C-14.');
  assert.equal(t.text('#form-1 .ltd-cnt'), '41 / 500 Zeichen · nur Text');
  assert.equal(t.text('#form-1 .ltd-fmsg b'), 'Der Stand hat sich geändert. Bitte aktualisieren und erneut prüfen.');
  assert.equal(t.q('#form-1 .ltd-fmsg').getAttribute('role'), 'alert');
  assert.equal(t.q('#form-1 input[name="revision"]').getAttribute('value'), '4', 'the fresh revision, not the old one');
  assert.equal(t.doc.activeElement, reopened.querySelector('textarea'));
  assert.equal(t.q('.ltd-draft'), null);
  assert.equal(t.controller.state().hold, 'editing');
  assert.equal(t.q('#form-1 button[type="submit"]').hasAttribute('disabled'), false);
  assert.equal(t.net.posts().length, 1, 'nothing is sent again');

  // 422 invalid keeps the draft too, with the server's own text.
  const v = setup();
  const fv = v.open(2);
  v.type(fv.querySelector('textarea'), 'x'.repeat(501));
  assert.equal(v.text('#cnt-2'), '501 / 500 Zeichen · nur Text');
  assert.ok(v.q('#cnt-2').classList.contains('is-over'), 'the counter marks the excess');
  v.click(v.q('#form-2 button[type="submit"]'));
  await flush();
  v.net.posts()[0].respond(422, {
    error: 'invalid',
    message: 'Kommentar ist zu lang: 501 von höchstens 500 Zeichen. Nichts wurde gespeichert.',
    draft_target: true,
    fragment: v.fragment({ asOf: 12 })
  });
  await flush();
  assert.equal(v.q('#c-2').value.length, 501);
  assert.equal(v.text('#form-2 .ltd-fmsg b'), 'Kommentar ist zu lang: 501 von höchstens 500 Zeichen. Nichts wurde gespeichert.');
  assert.ok(v.q('#cnt-2').classList.contains('is-over'));

  // The row left the result (or the action is gone): the draft is a read-only card.
  const g = setup({ panel: { rows: [row(1), row(2)] } });
  const fg = g.open(1);
  g.type(fg.querySelector('textarea'), 'Komet ist am Platz');
  g.q('#lt-row-1 .ltd-mlink').focus();
  g.click(g.q('#form-1 button[type="submit"]'));
  await flush();
  g.net.posts()[0].respond(409, {
    error: 'refused',
    message: 'Das Turnier wurde um 14:52 pausiert.',
    draft_target: false,
    fragment: g.fragment({ asOf: 20, rows: [row(2)] })
  });
  await flush();
  assert.equal(g.q('#lt-row-1'), null);
  assert.equal(g.q('form[data-ack][data-open]'), null);
  assert.equal(g.text('.ltd-ban.b-err .ltd-banh'), '!Nicht festgehalten: Das Turnier wurde um 14:52 pausiert.');
  assert.equal(g.text('.ltd-ban.b-err .ltd-banb'), LABELS.ack_refused_detail);
  assert.equal(g.text('.ltd-draft h2'), LABELS.draft_heading);
  assert.equal(g.q('.ltd-draft textarea').value, 'Komet ist am Platz');
  assert.equal(g.q('.ltd-draft textarea').hasAttribute('readonly'), true);
  assert.equal(g.root.getAttribute('data-as-of'), iso(20));
  assert.equal(g.controller.state().hold, null, 'no form is open: polling resumes');
  assert.equal(g.net.posts().length, 1);
  await g.tick(30000);
  assert.equal(g.net.gets().length, 1);

  // The row stays but the action is gone (paused): card, and the row says why.
  const p = setup({ panel: { rows: [row(1)] } });
  const fp = p.open(1);
  p.type(fp.querySelector('textarea'), 'Entwurf eins');
  p.click(p.q('#form-1 button[type="submit"]'));
  await flush();
  p.net.posts()[0].respond(409, {
    error: 'refused',
    message: 'Pausiert.',
    draft_target: false,
    fragment: p.fragment({ asOf: 20, rows: [row(1, { ack: false })] })
  });
  await flush();
  assert.equal(p.q('form[data-ack]'), null);
  assert.equal(p.text('#lt-row-1 .ltd-why'), 'Pausiert – Prüfen nicht möglich.');
  assert.equal(p.q('.ltd-draft textarea').value, 'Entwurf eins');

  // A stale pin shows next to the pin, after the whole panel was replaced.
  const pin = setup();
  pin.click(pin.q('#lt-row-2 button[data-pin]'));
  await flush();
  assert.equal(pin.q('#lt-row-2 button[data-pin]').textContent, LABELS.pin_adding);
  assert.equal(pin.q('#lt-row-2 button[data-pin]').hasAttribute('disabled'), true);
  pin.net.posts()[0].respond(409, {
    error: 'stale',
    message: 'Der Stand hat sich geändert. Bitte aktualisieren und erneut versuchen.',
    draft_target: false,
    fragment: pin.fragment({ asOf: 40, rows: [row(1), row(2, { pinned: true }), row(3)] })
  });
  await flush();
  assert.equal(pin.text('#lt-row-2 .ltd-err'), 'Der Stand hat sich geändert. Bitte aktualisieren und erneut versuchen.');
  assert.equal(pin.q('#lt-row-2 .ltd-err').getAttribute('role'), 'alert');
  assert.equal(pin.root.getAttribute('data-as-of'), iso(40));
  assert.equal(pin.doc.activeElement, pin.q('#lt-row-2 button[data-pin]'));

  // Text from the answer is text, never markup.
  const x = setup();
  const fx = x.open(1);
  x.type(fx.querySelector('textarea'), 'y');
  x.click(x.q('#form-1 button[type="submit"]'));
  await flush();
  x.net.posts()[0].respond(422, { error: 'invalid', message: '<img src=x onerror=alert(1)>', draft_target: true, fragment: x.fragment({ asOf: 3 }) });
  await flush();
  assert.equal(x.text('#form-1 .ltd-fmsg b'), '<img src=x onerror=alert(1)>');
  assert.equal(x.qa('img').length, 0);
});

test('test_manual_refresh_with_draft_asks_first', async () => {
  // Nothing typed: the refresh goes straight out and nothing is asked.
  const quiet = setup();
  quiet.click(quiet.q('.ltd-refresh button'));
  await flush();
  assert.equal(quiet.net.calls.length, 1);
  assert.equal(quiet.text('.ltd-fst .ltd-fst-auto'), LABELS.fresh_loading);
  assert.ok(quiet.q('.ltd-fst > .ltd-spin'), 'the spinner is an item of the status line');
  await quiet.answer(quiet.net.calls[0], { asOf: 4 });
  assert.equal(quiet.text('[data-live]'), 'Aktualisiert, Stand 14:50:04');

  // An open draft: the question is inline, never a browser dialog.
  const t = setup({ panel: { rows: [row(1), row(2)] } });
  const form = t.open(1);
  t.type(form.querySelector('textarea'), 'Komet ist am Platz, Nori hängt noch im Kupfer-Cup');
  t.click(t.q('.ltd-refresh button'));
  await flush();
  assert.equal(t.net.calls.length, 0);
  assert.equal(t.text('.ltd-ban .ltd-banh'), `!${LABELS.refresh_confirm}`);
  const yes = t.q('[data-ltd-js-yes]');
  assert.equal(yes.textContent, LABELS.ack_refresh_keep);
  assert.equal(t.q('[data-ltd-js-no]').textContent, LABELS.ack_cancel);
  assert.equal(t.doc.activeElement, yes);
  assert.equal(t.q('#form-1').hasAttribute('data-open'), true, 'the form stays');
  assert.equal(t.q('#c-1').value, 'Komet ist am Platz, Nori hängt noch im Kupfer-Cup');
  // A second click does not stack a second question.
  t.click(t.q('.ltd-refresh button'));
  assert.equal(t.qa('[data-ltd-js="confirm"]').length, 1);

  // No: back to the button, nothing was fetched.
  t.click(t.q('[data-ltd-js-no]'));
  assert.equal(t.q('[data-ltd-js="confirm"]'), null);
  assert.equal(t.doc.activeElement, t.q('.ltd-refresh button'));
  assert.equal(t.net.calls.length, 0);
  assert.equal(t.q('#c-1').value, 'Komet ist am Platz, Nori hängt noch im Kupfer-Cup');

  // Yes: the list is fetched and the draft returns into the new form.
  t.click(t.q('.ltd-refresh button'));
  t.click(t.q('[data-ltd-js-yes]'));
  await flush();
  assert.equal(t.net.calls.length, 1);
  assert.equal(t.net.calls[0].method, 'GET');
  assert.equal(t.q('[data-ltd-js="confirm"]'), null);
  await t.answer(t.net.calls[0], {
    asOf: 30,
    rows: [row(1, { record: 'Zuletzt geprüft von Jonas um 14:51', revision: 5 }), row(2, { tierName: 'Neu' })]
  });
  assert.equal(t.root.getAttribute('data-as-of'), iso(30));
  assert.equal(t.text('#lt-row-2 .ltd-tn'), 'Neu');
  const kept = t.q('#form-1');
  assert.equal(kept.hasAttribute('data-open'), true);
  assert.equal(kept.querySelector('textarea').value, 'Komet ist am Platz, Nori hängt noch im Kupfer-Cup');
  assert.equal(kept.querySelector('input[name="revision"]').getAttribute('value'), '5');
  assert.equal(t.text('#cnt-1'), '49 / 500 Zeichen · nur Text');
  assert.equal(t.controller.state().hold, 'editing');
  assert.equal(t.net.posts().length, 0, 'the draft is not sent by the refresh');

  // The draft's row left the list: the draft is not lost.
  const gone = setup({ panel: { rows: [row(1), row(2)] } });
  const fg = gone.open(1);
  gone.type(fg.querySelector('textarea'), 'Mein Entwurf');
  gone.click(gone.q('.ltd-refresh button'));
  gone.click(gone.q('[data-ltd-js-yes]'));
  await flush();
  await gone.answer(gone.net.calls[0], { asOf: 30, rows: [row(2)] });
  assert.equal(gone.q('#lt-row-1'), null);
  assert.equal(gone.q('.ltd-draft textarea').value, 'Mein Entwurf');
  assert.equal(gone.controller.state().hold, null);

  // Answering the question when the draft is gone from the form leaves no stale question.
  const empty = setup();
  const fe = empty.open(1);
  empty.type(fe.querySelector('textarea'), 'abc');
  empty.click(empty.q('.ltd-refresh button'));
  assert.ok(empty.q('[data-ltd-js="confirm"]'));
  empty.click(empty.q('#form-1 button[data-ack-cancel]'));
  assert.equal(empty.q('[data-ltd-js="confirm"]'), null);
  await flush();
  empty.net.calls.forEach((c) => c.fail());
  await flush();
  empty.click(empty.q('.ltd-refresh button'));
  await flush();
  assert.ok(empty.net.calls.length >= 2, 'the refresh asks nothing any more');
});

test('test_gateway_5xx_and_csrf_errors_are_not_access_loss', async () => {
  const outages = [
    ['502 HTML', (c) => c.html(502, '<html>Bad Gateway</html>')],
    ['503 HTML', (c) => c.html(503, '<html>Service Unavailable</html>')],
    ['500 JSON', (c) => c.respond(500, { error: 'boom' })],
    ['500 JSON naming the session', (c) => c.respond(500, { error: 'session_expired' })],
    ['200 HTML, not a redirect', (c) => c.html(200, '<html>maintenance</html>')],
    ['200 JSON without a panel', (c) => c.respond(200, { hello: 'world' })],
    ['200 with a broken answer', (c) => c.html(200, '{not json')],
    ['404 JSON', (c) => c.respond(404, { error: 'unavailable' })],
    ['429', (c) => c.html(429, 'slow down')]
  ];
  for (const [name, answer] of outages) {
    const t = setup();
    await t.tick(30000);
    answer(t.net.calls[0]);
    await flush();
    assert.equal(t.qa('li.ltd-row').length, 3, name);
    assert.equal(t.q('.ltd-sum') !== null, true, name);
    assert.equal(t.controller.state().stopped, false, name);
    assert.equal(t.controller.state().failed, true, name);
    assert.equal(t.text('.ltd-ban .ltd-banh'), `!${LABELS.fresh_failed}`, name);
    assert.equal(t.q('.ltd-ban a'), null, name);
    await t.tick(30000);
    assert.equal(t.net.calls.length, 2, `${name}: polling goes on`);
  }

  // A rejected token on a submit: the notice, the draft and the form stay; nothing is lost.
  const t = setup({ panel: { rows: [row(1), row(2)] } });
  const form = t.open(1);
  t.type(form.querySelector('textarea'), 'Mein Kommentar');
  t.click(t.q('#form-1 button[type="submit"]'));
  await flush();
  t.net.posts()[0].respond(403, { error: 'csrf_invalid', message: LABELS.csrf_invalid });
  await flush();
  assert.equal(t.qa('li.ltd-row').length, 2, 'the data stays');
  assert.equal(t.text('.ltd-ban.b-err .ltd-banh'), `!${LABELS.csrf_invalid}`);
  assert.equal(t.q('.ltd-sum') !== null, true);
  assert.equal(t.doc.getElementById('form-1'), form, 'the same form');
  assert.equal(t.q('#c-1').value, 'Mein Kommentar');
  assert.equal(t.q('#c-1').hasAttribute('readonly'), false);
  assert.equal(t.q('#form-1 button[type="submit"]').hasAttribute('disabled'), false);
  assert.equal(t.controller.state().stopped, false);
  assert.equal(t.q('.ltd-draft'), null, 'no unsent card: the form still holds it');
  assert.equal(t.net.posts().length, 1, 'no second attempt on its own');
  // Closing the form lets the polling continue.
  t.click(t.q('#form-1 button[data-ack-cancel]'));
  await flush();
  assert.equal(t.q('[data-ltd-js="csrf"]'), null, 'the notice goes with the form');
  assert.equal(t.net.gets().length, 1);
  assert.equal(t.controller.state().stopped, false);

  // A poll that answers `csrf_invalid` is no access loss either.
  const p = setup();
  await p.tick(30000);
  p.net.calls[0].respond(403, { error: 'csrf_invalid', message: 'x' });
  await flush();
  assert.equal(p.qa('li.ltd-row').length, 3);
  assert.equal(p.text('.ltd-ban .ltd-banh'), `!${LABELS.csrf_invalid}`);
  assert.equal(p.controller.state().stopped, false);
  await p.tick(30000);
  assert.equal(p.net.calls.length, 2, 'polling continues');

  // Any other 403 is the loss of access.
  for (const answer of [(c) => c.respond(403, { error: 'access_revoked' }), (c) => c.html(403, '<h1>Forbidden</h1>'), (c) => c.respond(403, {})]) {
    const q = setup();
    await q.tick(30000);
    answer(q.net.calls[0]);
    await flush();
    assert.equal(q.q('li.ltd-row'), null);
    assert.equal(q.controller.state().stopped, true);
  }

  // A failed submit is shown at the form and keeps the draft; the pin says so at the pin.
  const f = setup();
  const ff = f.open(1);
  f.type(ff.querySelector('textarea'), 'Entwurf');
  f.click(f.q('#form-1 button[type="submit"]'));
  await flush();
  f.net.posts()[0].html(502, '<html>Bad Gateway</html>');
  await flush();
  assert.equal(f.text('#form-1 .ltd-fmsg b'), LABELS.ack_failed);
  assert.equal(f.q('#c-1').value, 'Entwurf');
  assert.equal(f.q('#form-1 button[type="submit"]').hasAttribute('disabled'), false);
  assert.equal(f.q('#form-1 button[type="submit"]').textContent, 'Prüfung festhalten');
  assert.equal(f.controller.state().stopped, false);
  const g = setup();
  g.click(g.q('#lt-row-1 button[data-pin]'));
  await flush();
  g.net.posts()[0].fail();
  await flush();
  assert.equal(g.text('#lt-row-1 .ltd-err'), LABELS.pin_failed);
  assert.equal(g.q('#lt-row-1 button[data-pin]').textContent, 'Für das Orga-Team anpinnen');
  assert.equal(g.q('#lt-row-1 button[data-pin]').hasAttribute('disabled'), false);
  assert.equal(g.controller.state().stopped, false);
});

// ------------------------------------------------------------------ //
// the rest of the contract

test('test_classification_follows_the_documented_order', () => {
  const login = LOGIN;
  const table = [
    // [name, result, expected kind]
    ['network error', { failure: 'network' }, 'transient'],
    ['timeout', { failure: 'timeout' }, 'transient'],
    ['500', { status: 500, json: { error: 'x' } }, 'transient'],
    ['502 HTML', { status: 502, json: null }, 'transient'],
    ['503 naming the session', { status: 503, json: { error: 'session_expired' } }, 'transient'],
    ['200 panel', { status: 200, json: { html: 'x' } }, 'ok'],
    ['200 not JSON', { status: 200, json: null }, 'transient'],
    ['redirect to login', { status: 200, json: null, redirected: true, url: `http://h${login}?next=%2F` }, 'session'],
    ['redirect to login, 5xx beats it', { status: 502, json: null, redirected: true, url: `http://h${login}` }, 'transient'],
    ['redirect elsewhere', { status: 200, json: null, redirected: true, url: 'http://h/other' }, 'transient'],
    ['401', { status: 401, json: null }, 'session'],
    ['session_expired code', { status: 200, json: { error: 'session_expired' } }, 'session'],
    ['403 csrf', { status: 403, json: { error: 'csrf_invalid' } }, 'csrf'],
    ['403 revoked', { status: 403, json: { error: 'access_revoked' } }, 'access'],
    ['403 plain', { status: 403, json: null }, 'access'],
    ['404 unavailable', { status: 404, json: { error: 'unavailable' } }, 'refusal'],
    ['409 stale', { status: 409, json: { error: 'stale' } }, 'refusal'],
    ['409 refused', { status: 409, json: { error: 'refused' } }, 'refusal'],
    ['422 invalid', { status: 422, json: { error: 'invalid' } }, 'refusal'],
    ['409 unknown code', { status: 409, json: { error: 'weird' } }, 'transient'],
    ['400', { status: 400, json: null }, 'transient'],
    ['302 not followed', { status: 302, json: null }, 'transient']
  ];
  for (const [name, result, kind] of table) {
    assert.equal(classifyResponse(result, login).kind, kind, name);
  }
  assert.equal(classifyResponse({ status: 200, json: null, redirected: true, url: 'http://h/x' }, null).kind, 'transient');
});

test('test_overview_is_derived_from_the_poll_route', () => {
  assert.equal(overviewUrl('site', '/lan-tournaments/orga-dashboard/poll?view=due', 'https://h/'), '/lan-tournaments/');
  assert.equal(overviewUrl('admin', '/lan-tournaments/for_party/p-1/dashboard/poll?x=1', 'https://h/'), '/lan-tournaments/for_party/p-1');
});

test('test_handlers_are_bound_once_whatever_is_replaced', async () => {
  const t = setup();
  const counts = () => JSON.stringify(Object.entries(t.root.listeners).map(([k, v]) => [k, v.length]).sort());
  const before = counts();
  const docBefore = (t.doc.listeners.visibilitychange || []).length;
  assert.equal(initDashboard(t.root, t.options), t.controller, 'a second start returns the first controller');
  for (let i = 1; i <= 4; i++) {
    await t.tick(30000);
    await t.answer(t.net.calls[t.net.calls.length - 1], { asOf: 30 * i });
  }
  assert.equal(counts(), before);
  assert.equal((t.doc.listeners.visibilitychange || []).length, docBefore);
  assert.equal(t.qa('li.ltd-row *').filter((n) => Object.keys(n.listeners).length > 0).length, 0, 'no handler sits on a replaceable node');
  // One click, one effect, after four replacements.
  const form = t.open(2);
  assert.equal(t.qa('form[data-open]').length, 1);
  t.type(form.querySelector('textarea'), 'abc');
  assert.equal(t.text('#cnt-2'), '3 / 500 Zeichen · nur Text');
  t.click(t.q('#form-2 button[data-ack-cancel]'));
  assert.equal(t.qa('form[data-open]').length, 0);
  // After the stop nothing is bound to the document any more.
  t.controller.stop();
  assert.equal((t.doc.listeners.visibilitychange || []).length, docBefore - 1);
  assert.equal(t.clock.pending(), 0);
});

test('test_the_script_never_posts_on_its_own_and_never_decides', async () => {
  const t = setup({ panel: { rows: [row(1, { record: 'Zuletzt geprüft von Mara um 14:15' }), row(2)] } });
  const form = t.open(1);
  t.type(form.querySelector('textarea'), 'Entwurf');
  t.visibility(true);
  t.visibility(false);
  t.click(t.q('.ltd-refresh button'));
  t.click(t.q('[data-ltd-js-yes]'));
  await flush();
  await t.answer(t.net.calls[0], { asOf: 30 });
  await t.tick(600000);
  t.net.calls.forEach((c) => c.respond && !c.done && c.fail());
  await flush();
  assert.equal(t.net.posts().length, 0, 'a draft is only sent by the viewer');
  // The shown tier, colour and revision come from the server's markup only.
  const tier = t.q('#lt-row-2 .ltd-tier');
  const classes = tier.getAttribute('class');
  await t.tick(600000);
  assert.equal(t.q('#lt-row-2 .ltd-tier').getAttribute('class'), classes);
  assert.equal(t.q('#form-1 input[name="revision"]').getAttribute('value'), '1');
});

test('test_the_check_counts_code_points_and_the_history_stays_open', async () => {
  assert.equal(codePoints('abc'), 3);
  assert.equal(codePoints('a😀b'), 3);
  assert.equal(codePoints(''), 0);
  assert.equal(fill('%(a)s und %(b)d%%', { a: 'x', b: 2 }), 'x und 2%');
  assert.equal(fill('%(missing)s', {}), '%(missing)s');

  const t = setup({ panel: { rows: [row(1, { record: 'Zuletzt geprüft von Mara um 14:15', history: true }), row(2)] } });
  const toggle = t.q('#lt-row-1 button[data-hist]');
  toggle.focus();
  t.click(toggle);
  assert.equal(toggle.getAttribute('aria-expanded'), 'true');
  assert.equal(t.q('#hist-1').hasAttribute('hidden'), false);
  assert.equal(toggle.firstChild.nodeValue, `${LABELS.history_hide} `);
  assert.equal(t.doc.activeElement, toggle, 'the focus stays on the toggle');
  await t.tick(30000);
  await t.answer(t.net.calls[0], { asOf: 30, rows: [row(1, { record: 'Zuletzt geprüft von Mara um 14:15', history: true }), row(2)] });
  assert.equal(t.q('#lt-row-1 button[data-hist]'), toggle, 'the server said nothing new: the same toggle');
  assert.equal(toggle.getAttribute('aria-expanded'), 'true');
  assert.equal(t.doc.activeElement, toggle);
  // A new record replaces the part that holds the history.
  await t.tick(30000);
  await t.answer(t.net.calls[1], { asOf: 60, rows: [row(1, { record: 'Zuletzt geprüft von Jonas um 14:51', history: true }), row(2)] });
  const again = t.q('#lt-row-1 button[data-hist]');
  assert.notEqual(again, toggle);
  assert.equal(again.getAttribute('aria-expanded'), 'true', 'the disclosure survives the replacement');
  assert.equal(t.q('#hist-1').hasAttribute('hidden'), false);
  assert.equal(t.doc.activeElement, again, 'and so does the focus');
  t.click(again);
  assert.equal(t.q('#hist-1').hasAttribute('hidden'), true);
  assert.equal(again.firstChild.nodeValue, `${LABELS.history_show} `);
});

test('test_unapplied_filter_choices_survive_a_refresh', async () => {
  const t = setup({ panel: { surface: 'admin' }, path: ADMIN_LIST });
  const select = t.q('#lt-filter-sort');
  select.value = 'wait';
  t.doc.dispatch(select, 'change', {});
  t.q('input[value="all"]').checked = true;
  t.doc.dispatch(t.q('input[value="all"]'), 'change', {});
  select.focus();
  await t.tick(30000);
  await t.answer(t.net.calls[0], { asOf: 30, surface: 'admin' });
  assert.equal(t.q('#lt-filter-sort').value, 'wait');
  assert.equal(t.q('input[value="all"]').checked, true);
  assert.equal(t.q('input[value="assigned"]').checked, false);
  assert.equal(t.doc.activeElement, t.q('#lt-filter-sort'));
});

test('test_ack_and_pin_success_wait_for_the_server', async () => {
  const t = setup({ panel: { rows: [row(1), row(2)] } });
  t.click(t.q('#lt-row-2 button[data-pin]'));
  await flush();
  const post = t.net.posts()[0];
  assert.equal(post.url, `${SITE_LIST}/matches/2/pin`);
  assert.match(post.init.body, /(^|&)pinned=true(&|$)/);
  assert.match(post.init.body, /(^|&)revision=3(&|$)/);
  assert.equal(t.q('#lt-row-2 .ltd-okmsg'), null);
  assert.equal(t.q('#lt-row-2 .ltd-sig.s-pin'), null, 'nothing shows as pinned before the server answers');
  assert.equal(t.controller.state().hold, 'submitting');
  post.respond(200, { committed_at: iso(30), fragment: t.fragment({ asOf: 30, rows: [row(1), row(2, { pinned: true, pinRevision: 4 })] }) });
  await flush();
  assert.equal(t.text('#lt-row-2 .ltd-okmsg'), 'Angepinnt von Mara um 14:03');
  assert.equal(t.q('#lt-row-2 .ltd-okmsg').getAttribute('role'), 'status');
  assert.equal(t.text('[data-live]'), 'Angepinnt von Mara um 14:03');
  assert.equal(t.doc.activeElement === t.doc.body || t.doc.activeElement === t.q('#lt-row-2 button[data-pin]'), true);
  assert.equal(t.q('#lt-row-2 button[data-pin]').textContent, 'Pin entfernen');
  assert.equal(t.root.getAttribute('data-as-of'), iso(30));

  // Unpin says so and keeps the focus on the button.
  t.q('#lt-row-2 button[data-pin]').focus();
  t.click(t.q('#lt-row-2 button[data-pin]'));
  await flush();
  const unpin = t.net.posts()[1];
  assert.match(unpin.init.body, /(^|&)pinned=false(&|$)/);
  assert.equal(t.q('#lt-row-2 button[data-pin]').textContent, LABELS.pin_removing);
  unpin.respond(200, { committed_at: iso(40), fragment: t.fragment({ asOf: 40, rows: [row(1), row(2)] }) });
  await flush();
  assert.equal(t.text('#lt-row-2 .ltd-okmsg'), LABELS.pin_removed);
  assert.equal(t.doc.activeElement, t.q('#lt-row-2 button[data-pin]'));

  // A reply without a usable panel claims nothing and asks the server again.
  const odd = setup();
  odd.click(odd.q('#lt-row-1 button[data-pin]'));
  await flush();
  odd.net.posts()[0].respond(200, { committed_at: iso(5) });
  await flush();
  assert.equal(odd.q('.ltd-okmsg'), null);
  assert.equal(odd.net.gets().length, 1, 'it reads the state back');
  assert.equal(odd.q('#lt-row-1 button[data-pin]').hasAttribute('disabled'), false);
});

test('test_an_open_draft_in_another_row_survives_a_save', async () => {
  const t = setup({ panel: { rows: [row(1), row(2)] } });
  const other = t.open(2);
  t.type(other.querySelector('textarea'), 'Zweiter Entwurf');
  const mine = t.open(1);
  t.type(mine.querySelector('textarea'), 'Erster Entwurf');
  t.click(t.q('#form-1 button[type="submit"]'));
  await flush();
  t.net.posts()[0].respond(200, {
    committed_at: iso(30),
    fragment: t.fragment({ asOf: 30, rows: [row(1, { record: 'Zuletzt geprüft von Jonas um 14:52' }), row(2)] })
  });
  await flush();
  assert.equal(t.q('#form-1').hasAttribute('data-open'), false);
  assert.equal(t.q('#form-2').hasAttribute('data-open'), true);
  assert.equal(t.q('#c-2').value, 'Zweiter Entwurf');
  assert.equal(t.controller.state().hold, 'editing');
});

test('test_the_poll_interval_follows_the_server_and_a_hung_request_times_out', async () => {
  const t = setup({ panel: { pollSeconds: 10 } });
  await t.tick(9999);
  assert.equal(t.net.calls.length, 0);
  await t.tick(1);
  assert.equal(t.net.calls.length, 1);
  await t.answer(t.net.calls[0], { asOf: 10, pollSeconds: 45 });
  assert.equal(t.text('.ltd-fst-auto'), 'Automatisch alle 45 s');
  await t.tick(44999);
  assert.equal(t.net.calls.length, 1);
  await t.tick(1);
  assert.equal(t.net.calls.length, 2);
  await t.tick(14999);
  assert.equal(t.net.calls[1].signal.aborted, false);
  await t.tick(1);
  assert.equal(t.net.calls[1].signal.aborted, true);
  await flush();
  assert.equal(t.controller.state().failed, true);
  assert.equal(t.controller.state().stopped, false);
  await t.tick(45000);
  assert.equal(t.net.calls.length, 3, 'the next attempt follows');
});

test('test_hooks_name_what_the_script_reads', () => {
  const t = setup({ panel: { rows: [row(1, { record: 'Zuletzt geprüft von Mara um 14:15', history: true, pinned: true })], surface: 'admin' }, path: ADMIN_LIST });
  const optional = new Set([]);
  Object.entries(HOOKS).forEach(([name, selector]) => {
    if (optional.has(name)) return;
    assert.ok(t.root.querySelector(selector) !== null, `${name}: ${selector}`);
  });
});

test('test_a_manual_refresh_keeps_an_open_form_without_a_draft', async () => {
  const t = setup({ panel: { rows: [row(1), row(2)] } });
  t.open(1);
  t.click(t.q('.ltd-refresh button'));
  await flush();
  assert.equal(t.q('[data-ltd-js="confirm"]'), null, 'nothing to lose, nothing to ask');
  assert.equal(t.net.calls.length, 1);
  await t.answer(t.net.calls[0], { asOf: 12, rows: [row(1, { revision: 7 }), row(2)] });
  assert.equal(t.root.getAttribute('data-as-of'), iso(12), 'the list was refreshed');
  assert.equal(t.q('#form-1').hasAttribute('data-open'), true, 'and the form is still open');
  assert.equal(t.q('#form-1 input[name="revision"]').getAttribute('value'), '7');
  assert.equal(t.controller.state().hold, 'editing');
});

test('test_the_retry_of_a_failed_refresh_asks_first_when_a_draft_is_open', async () => {
  const t = setup({ panel: { rows: [row(1), row(2)] } });
  await t.tick(30000);
  t.net.calls[0].fail();
  await flush();
  const form = t.open(1);
  t.type(form.querySelector('textarea'), 'Entwurf');
  t.click(t.q('.ltd-ban [data-ltd-js-retry]'));
  await flush();
  assert.equal(t.net.calls.length, 1, 'no request before the answer');
  assert.ok(t.q('[data-ltd-js="confirm"]'));
  assert.equal(t.q('#c-1').value, 'Entwurf');
});

test('test_a_hung_submit_ends_with_the_draft_kept_and_the_form_usable', async () => {
  const t = setup();
  const form = t.open(1);
  t.type(form.querySelector('textarea'), 'Entwurf');
  t.click(t.q('#form-1 button[type="submit"]'));
  await flush();
  await t.tick(15000);
  await flush();
  assert.equal(t.net.posts()[0].signal.aborted, true);
  assert.equal(t.text('#form-1 .ltd-fmsg b'), LABELS.ack_failed);
  assert.equal(t.q('#c-1').value, 'Entwurf');
  assert.equal(t.q('#form-1 button[type="submit"]').hasAttribute('disabled'), false);
  assert.equal(t.controller.state().stopped, false);
});

// The panel as the real templates render it (the unit tests on the Python
// side hand it over in `LT_DASHBOARD_PANEL`); without it a built-in copy of
// the markup is used, so this test never skips.
test('test_the_script_runs_on_the_real_markup', async (context) => {
  const file = process.env.LT_DASHBOARD_PANEL;
  const html = file
    ? fs.readFileSync(file, 'utf8')
    : panel({
        rows: [row(1, { record: 'Zuletzt geprüft von Mara um 14:15', history: true, pinned: true }), row(2), row(3)]
      });
  const probe = new FakeDocument();
  parseInto(probe, html, probe.body);
  const shown = probe.body.querySelector('[data-lt-dashboard-root]');
  const pollPath = new URL(shown.getAttribute('data-poll-url'), 'https://lan.example').pathname;
  const listPath = pollPath.replace(/\/poll$/, '');
  const t = setup({ html, path: listPath, search: '?view=due' });
  const asOf = shown.getAttribute('data-as-of');
  const seconds = Number(shown.getAttribute('data-poll-seconds'));
  const rows = t.qa('li.ltd-row').length;
  context.diagnostic(`real-markup rows=${rows} source=${file ? 'real' : 'builtin'}`);
  assert.ok(t.controller, 'the script starts on this markup');
  assert.equal(t.root.classList.contains('is-js'), true);
  assert.ok(t.qa('button[data-ack-open]').length > 0);
  assert.equal(t.qa('button[data-ack-open]').every((b) => !b.hasAttribute('hidden')), true);
  assert.equal(t.qa('form[data-ack] button[data-ack-cancel]').every((b) => b.hasAttribute('hidden')), true);
  assert.ok(t.text('.ltd-fst .ltd-fst-auto').length > 0, 'the status line has its text');

  // A poll: one GET at the URL the server named; the answer replaces the panel.
  await t.tick(seconds * 1000);
  assert.equal(t.net.calls.length, 1);
  assert.equal(t.net.calls[0].method, 'GET');
  assert.equal(new URL(t.net.calls[0].url, 'https://lan.example').pathname, pollPath);
  assert.equal(t.net.calls[0].url, shown.getAttribute('data-poll-url'));
  const first = t.q('li.ltd-row');
  t.net.calls[0].respond(200, { html, as_of: asOf, poll_seconds: seconds });
  await flush();
  assert.equal(t.q('li.ltd-row'), first, 'the server said nothing new: the rows are the same nodes');
  assert.equal(t.qa('li.ltd-row').length, rows);
  assert.equal(t.root.getAttribute('data-poll-url'), shown.getAttribute('data-poll-url'));
  assert.equal(t.controller.state().failed, false);

  // The check on the first row that offers one: the form, its fields, the post.
  const opener = t.q('button[data-ack-open]');
  t.click(opener);
  const form = t.doc.getElementById(opener.getAttribute('aria-controls'));
  assert.equal(form.hasAttribute('data-open'), true);
  const area = form.querySelector('textarea');
  assert.equal(t.doc.activeElement, area);
  t.type(area, 'Beide Captains kontaktiert');
  assert.match(form.querySelector('.ltd-cnt').textContent, /^26 \/ 500\b/);
  const names = form.querySelectorAll('input[name]').map((i) => i.getAttribute('name'));
  assert.ok(names.includes('csrf_token') && names.includes('episode') && names.includes('revision'));
  t.click(form.querySelector('button[type="submit"]'));
  await flush();
  const post = t.net.posts()[0];
  assert.equal(post.url, new URL(form.getAttribute('action'), 'https://lan.example').pathname);
  names.concat('comment').forEach((name) => assert.match(post.init.body, new RegExp(`(^|&)${name}=`), name));
  assert.match(post.init.body, /comment=Beide\+Captains\+kontaktiert/);
  assert.equal(t.net.gets().length, 1);
  post.respond(409, { error: 'stale', message: 'Der Stand hat sich geändert.', draft_target: true, fragment: { html, as_of: asOf, poll_seconds: seconds } });
  await flush();
  const again = t.doc.getElementById(form.id);
  assert.notEqual(again, form);
  assert.equal(again.hasAttribute('data-open'), true);
  assert.equal(again.querySelector('textarea').value, 'Beide Captains kontaktiert');
  assert.equal(again.querySelector('.ltd-fmsg b').textContent, 'Der Stand hat sich geändert.');
  t.click(again.querySelector('button[data-ack-cancel]'));
  assert.equal(again.hasAttribute('data-open'), false);
  await flush();

  // A tile asks for its own query on the poll route.
  const link = t.q('.ltd-sum li.ltd-st a[href]');
  if (link !== null) {
    const query = new URL(link.getAttribute('href'), 'https://lan.example').search;
    t.click(link);
    await flush();
    const tileCall = t.net.gets()[t.net.gets().length - 1];
    assert.equal(tileCall.url, `${pollPath}${query}`);
  }
});

test('test_a_dropped_request_never_replaces_the_panel_even_if_its_answer_arrives', async () => {
  // A `fetch` that ignores the abort signal: the answer of the dropped
  // request still arrives, and it is newer than everything on screen.
  const calls = [];
  const deaf = (url, init) =>
    new Promise((resolve) => {
      calls.push({ url, init, respond: (status, body) => resolve(response(status, body)) });
    });
  const t = setup({ options: { fetch: deaf } });
  await t.tick(30000);
  assert.equal(calls.length, 1);
  t.click(t.q('li.ltd-st.t-y a'));
  await flush();
  assert.equal(calls.length, 2, 'the tile asks for its own query');
  calls[1].respond(200, t.fragment({ asOf: 5, tiles: { r: 0, y: 3, g: 0, active: 'y' }, query: '?view=due&state=tier-yellow' }));
  await flush();
  assert.equal(t.root.getAttribute('data-as-of'), iso(5));
  calls[0].respond(200, t.fragment({ asOf: 900, rows: [row(9)] }));
  await flush();
  assert.equal(t.root.getAttribute('data-as-of'), iso(5), 'the dropped answer is not shown');
  assert.ok(!rowIds(t).includes('lt-row-9'));
  assert.equal(t.controller.state().failed, false);
});

test('test_the_status_line_is_written_only_when_its_words_change', async () => {
  const t = setup();
  const line = () => t.q('.ltd-fst-auto');
  assert.equal(line().writes, undefined, 'the server\'s words stay as they are at start');
  await t.tick(30000);
  await t.answer(t.net.calls[0], { asOf: 30 });
  assert.equal(line().writes, undefined, 'a quiet poll does not make the live region speak');
  await t.tick(30000);
  t.net.calls[1].fail();
  await flush();
  assert.equal(line().writes, 1, 'the failure is one change');
  await t.tick(30000);
  t.net.calls[2].fail();
  await flush();
  assert.equal(line().writes, 1, 'the second failure says nothing new');
  assert.equal(t.q('.ltd-fst > .ltd-spin'), null);
});

test('test_a_form_that_does_not_post_to_this_origin_is_left_to_the_browser', async () => {
  const t = setup();
  const form = t.open(1);
  form.setAttribute('action', 'https://elsewhere.example/ack');
  t.type(form.querySelector('textarea'), 'Entwurf');
  t.click(t.q('#form-1 button[type="submit"]'));
  await flush();
  assert.equal(t.net.posts().length, 0, 'nothing is sent from here');
  assert.equal(t.nativeSubmits.length, 1, 'the browser does what the markup says');
  assert.equal(t.controller.state().hold, 'editing');
  assert.equal(t.q('#form-1 button[type="submit"]').hasAttribute('disabled'), false);
});

// ------------------------------------------------------------------ //
// keeping the reading position: a swap touches only what the server changed

// Record every insert, move, removal and attribute write under `root`, with
// the node hit (and the attribute named).
function watchStructure(root) {
  const seen = [];
  const names = ['insertBefore', 'removeChild', 'appendChild', 'setAttribute', 'removeAttribute'];
  const saved = names.map((name) => [name, FakeElement.prototype[name]]);
  saved.forEach(([name, original]) => {
    FakeElement.prototype[name] = function (...args) {
      if (root.contains(this)) seen.push({ name, target: this, attr: /Attribute$/.test(name) ? args[0] : undefined });
      return original.apply(this, args);
    };
  });
  return {
    seen,
    stop() {
      saved.forEach(([name, original]) => {
        FakeElement.prototype[name] = original;
      });
    }
  };
}

const same = (a, b) => a.length === b.length && a.every((node, i) => node === b[i]);

test('test_a_poll_without_changes_keeps_every_node_and_only_the_freshness_moves', async () => {
  const content = {
    rows: [row(1, { record: 'Zuletzt geprüft von Mara um 14:15', history: true, pinned: true }), row(2), row(3)],
    chips: ['Sortierung: Dringlichkeit'],
    pager: [1, 2]
  };
  const t = setup({ panel: content });
  const toggle = t.q('#lt-row-1 button[data-hist]');
  t.click(toggle);
  // The link in the actions has the address of the title link; a restore by
  // address would move the focus to the title.
  const link = t.q('#lt-row-2 .ltd-links a');
  assert.equal(link.getAttribute('href'), t.q('#lt-row-2 .ltd-mlink').getAttribute('href'));
  link.focus();
  const everything = t.root.descendants();
  const groups = ['li.ltd-row', 'li.ltd-st', 'h2', 'h3', '.ltd-sum', '.ltd-count', 'ol.ltd-rows', 'form.ltd-filters', '.ltd-top', 'nav.ltd-pager', '.ltd-sr'].map((selector) => [
    selector,
    t.qa(selector)
  ]);
  assert.ok(everything.length > 100, 'the panel is a real tree');
  assert.equal(t.qa('li.ltd-row').length, 3);
  const freshness = t.q('.ltd-fresh b');
  assert.equal(freshness.textContent, '14:50:00');

  await t.tick(30000);
  const watch = watchStructure(t.root);
  try {
    await t.answer(t.net.calls[0], Object.assign({ asOf: 30 }, content));
  } finally {
    watch.stop();
  }

  // Every element is the same object, in the same place.
  assert.ok(same(t.root.descendants(), everything), 'no node was replaced, moved or added');
  groups.forEach(([selector, before]) => assert.ok(same(t.qa(selector), before), selector));
  // The one thing that moved is the freshness.
  assert.equal(t.q('.ltd-fresh b'), freshness);
  assert.equal(freshness.textContent, '14:50:30');
  assert.equal(t.root.getAttribute('data-as-of'), iso(30));
  const attributeWrites = watch.seen.filter((c) => c.attr !== undefined);
  assert.deepEqual(
    attributeWrites.map((c) => [c.target === t.root, c.attr]),
    [[true, 'data-as-of']],
    'not one attribute is written again with its own value'
  );
  const structure = watch.seen.filter((c) => c.attr === undefined);
  assert.ok(structure.length > 0 && structure.every((c) => c.target === freshness), 'only the time was written');
  // The live regions stay quiet.
  assert.equal(t.q('[data-live]').writes, undefined);
  assert.equal(t.text('[data-live]'), '');
  assert.equal(t.q('.ltd-fst-auto').writes, undefined);
  assert.equal(t.q('.ltd-ban'), null);
  // What the viewer was doing stays where it was.
  assert.equal(t.doc.activeElement, link);
  assert.equal(toggle.getAttribute('aria-expanded'), 'true');
  assert.equal(t.q('#hist-1').hasAttribute('hidden'), false);
  assert.equal(t.root.classList.contains('is-js'), true);
  assert.equal(t.q('button[data-ack-open]').hasAttribute('hidden'), false);
  assert.equal(t.controller.state().failed, false);
  assert.equal(t.controller.state().shownAsOf, T0 + 30000);
});

test('test_a_one_row_change_replaces_only_that_row_and_an_open_form_in_another_row_keeps_draft_and_focus', async () => {
  const t = setup({ panel: { rows: [row(1), row(2), row(3)] } });
  const form = t.open(3);
  const area = form.querySelector('textarea');
  t.type(area, 'Entwurf in Zeile drei');
  const everyNodeOf = (n) => [t.q(`#lt-row-${n}`)].concat(t.q(`#lt-row-${n}`).descendants());
  const others = { 2: everyNodeOf(2), 3: everyNodeOf(3) };
  const heading = t.q('#ltd-count');
  const tiles = t.q('.ltd-sum');
  const old = { tier: t.q('#lt-row-1 .ltd-tier'), times: t.q('#lt-row-1 .ltd-times') };

  // The draft is asked about first; the refresh then keeps the open form.
  t.click(t.q('.ltd-refresh button'));
  t.click(t.q('[data-ltd-js-yes]'));
  await flush();
  area.focus();
  await t.answer(t.net.calls[0], {
    asOf: 30,
    rows: [row(1, { tier: 'r', tierName: 'Lange Verzögerung', wait: '25 min' }), row(2), row(3)]
  });

  assert.equal(t.text('#lt-row-1 .ltd-tn'), 'Lange Verzögerung');
  assert.equal(t.text('#lt-row-1 .ltd-times dd'), '25 min');
  assert.notEqual(t.q('#lt-row-1 .ltd-tier'), old.tier, 'the changed part is new');
  assert.notEqual(t.q('#lt-row-1 .ltd-times'), old.times);
  [2, 3].forEach((n) => assert.ok(same(everyNodeOf(n), others[n]), `row ${n} is untouched`));
  assert.equal(t.q('#ltd-count'), heading);
  assert.equal(t.q('.ltd-sum'), tiles);
  assert.equal(t.q('#form-3'), form, 'the open form is the same node');
  assert.equal(t.doc.getElementById('c-3'), area);
  assert.equal(area.value, 'Entwurf in Zeile drei');
  assert.equal(area.valueWrites, 1, 'only the viewer typed into the field; a write could move the caret');
  assert.equal(form.hasAttribute('data-open'), true);
  assert.equal(t.doc.activeElement, area, 'the focus is where the viewer left it');
  assert.equal(t.text('#cnt-3'), '21 / 500 Zeichen · nur Text');
  assert.equal(t.controller.state().hold, 'editing');
  assert.equal(t.net.posts().length, 0);

  // The row of the open form itself changes: the form is rebuilt from the
  // server's markup and gets the draft back.
  t.click(t.q('.ltd-refresh button'));
  t.click(t.q('[data-ltd-js-yes]'));
  await flush();
  t.q('#c-3').focus();
  await t.answer(t.net.calls[1], { asOf: 60, rows: [row(1, { tier: 'r', tierName: 'Lange Verzögerung', wait: '25 min' }), row(2), row(3, { revision: 2 })] });
  assert.notEqual(t.q('#form-3'), form, 'its check part changed on the server');
  assert.equal(t.q('#form-3').hasAttribute('data-open'), true);
  assert.equal(t.q('#c-3').value, 'Entwurf in Zeile drei');
  assert.equal(t.q('#form-3 input[name="revision"]').getAttribute('value'), '2');
  assert.equal(t.doc.activeElement, t.q('#c-3'));
});

test('test_a_changed_row_keeps_the_parts_the_server_did_not_change', async () => {
  const t = setup({ panel: { rows: [row(1), row(2), row(3)] } });
  const partsOf = (n) => ['.ltd-tier', '.ltd-main', 'h3', '.ltd-times', '.ltd-act', '.ltd-ack'].map((selector) => t.q(`#lt-row-${n} ${selector}`));
  const li = t.q('#lt-row-2');
  const before = partsOf(2);
  const link = t.q('#lt-row-2 .ltd-mlink');
  link.focus();

  // The clock of the row ticked: the tier cell and the times, nothing else.
  await t.tick(30000);
  await t.answer(t.net.calls[0], { asOf: 30, rows: [row(1), row(2, { wait: '21 min' }), row(3)] });
  const after = partsOf(2);
  assert.equal(t.q('#lt-row-2'), li);
  assert.deepEqual(after.map((node, i) => node === before[i]), [false, true, true, false, true, true]);
  assert.equal(t.text('#lt-row-2 .ltd-tv b'), '21 min');
  assert.equal(t.text('#lt-row-2 .ltd-times dd'), '21 min');
  assert.equal(t.doc.activeElement, link, 'the reading position is the heading, which was not replaced');
  assert.deepEqual(t.qa('#lt-row-1 *').map((n) => n.hasAttribute('data-ltd-js')), t.qa('#lt-row-1 *').map(() => false));

  // The match changed: the main part, with its heading, is new; the focus follows by role.
  await t.tick(30000);
  const mid = partsOf(2);
  await t.answer(t.net.calls[1], { asOf: 60, rows: [row(1), row(2, { wait: '21 min', title: 'Team Neu gegen Team Alt' }), row(3)] });
  const end = partsOf(2);
  assert.deepEqual(end.map((node, i) => node === mid[i]), [true, false, false, true, true, true]);
  assert.equal(t.text('#lt-row-2 h3'), 'Team Neu gegen Team Alt');
  assert.equal(t.doc.activeElement, t.q('#lt-row-2 .ltd-mlink'));
  assert.notEqual(t.doc.activeElement, link);

  // A change in the row's own markup replaces the whole row.
  await t.tick(30000);
  await t.answer(t.net.calls[2], { asOf: 90, rows: [row(1), row(2, { wait: '21 min', title: 'Team Neu gegen Team Alt', rowClass: 'is-late' }), row(3)] });
  assert.notEqual(t.q('#lt-row-2'), li);
  assert.ok(t.q('#lt-row-2').classList.contains('is-late'));
});

test('test_added_and_removed_rows_land_in_server_order', async () => {
  const t = setup({ panel: { rows: [row(1), row(2), row(3)] } });
  const [one, , three] = t.qa('li.ltd-row');
  const ids = () => rowIds(t);

  // Row 2 went, rows 4 and 5 came: one in front, one at the end.
  await t.tick(30000);
  await t.answer(t.net.calls[0], { asOf: 30, rows: [row(4), row(1), row(3), row(5)] });
  assert.deepEqual(ids(), ['lt-row-4', 'lt-row-1', 'lt-row-3', 'lt-row-5']);
  assert.equal(t.q('#lt-row-1'), one, 'a row that stays is the same node');
  assert.equal(t.q('#lt-row-3'), three);
  assert.equal(t.q('#lt-row-2'), null);
  assert.equal(t.q('ol.ltd-rows').childNodes.length, 4);

  // The server's order wins, and the rows are moved, not rebuilt.
  const nodes = t.qa('li.ltd-row');
  await t.tick(30000);
  await t.answer(t.net.calls[1], { asOf: 60, rows: [row(5), row(3), row(1), row(4)] });
  assert.deepEqual(ids(), ['lt-row-5', 'lt-row-3', 'lt-row-1', 'lt-row-4']);
  assert.ok(same(t.qa('li.ltd-row'), [nodes[3], nodes[2], nodes[1], nodes[0]]));

  // One row in the middle, then none: the empty list leaves no rows behind.
  await t.tick(30000);
  await t.answer(t.net.calls[2], { asOf: 90, rows: [row(1)] });
  assert.deepEqual(ids(), ['lt-row-1']);
  assert.equal(t.q('#lt-row-1'), nodes[1]);
  await t.tick(30000);
  await t.answer(t.net.calls[3], { asOf: 120, rows: [], count: '0 Begegnungen' });
  assert.deepEqual(ids(), []);
  assert.equal(t.text('#ltd-count'), '0 Begegnungen');
  await t.tick(30000);
  await t.answer(t.net.calls[4], { asOf: 150, rows: [row(7), row(6)] });
  assert.deepEqual(ids(), ['lt-row-7', 'lt-row-6']);
  assert.ok(t.q('ol.ltd-rows'), 'the list is back');
});

test('test_tiles_counts_filter_chips_and_paging_are_replaced_as_units', async () => {
  const rows = [row(1), row(2)];
  const content = { rows, chips: ['Stufe: Gelb'], pager: [1, 2], tiles: { r: 0, y: 2, g: 0, active: null } };
  const t = setup({ panel: content });
  const unit = () => ({
    sum: t.q('.ltd-sum'),
    count: t.q('.ltd-count'),
    filters: t.q('form.ltd-filters'),
    pager: t.q('nav.ltd-pager'),
    top: t.q('.ltd-top'),
    list: t.q('ol.ltd-rows')
  });
  const was = unit();
  const rowNodes = t.qa('li.ltd-row');
  const countHeading = t.q('#ltd-count');

  // A tile count moved: the tiles are new, everything else stays.
  await t.tick(30000);
  await t.answer(t.net.calls[0], Object.assign({}, content, { asOf: 30, tiles: { r: 1, y: 1, g: 0, active: null } }));
  let now = unit();
  assert.notEqual(now.sum, was.sum);
  assert.equal(t.text('li.ltd-st.t-r .ltd-stc'), '1');
  ['count', 'filters', 'pager', 'top', 'list'].forEach((key) => assert.equal(now[key], was[key], key));
  assert.ok(same(t.qa('li.ltd-row'), rowNodes));

  // The count changed: its heading is new.
  await t.tick(30000);
  await t.answer(t.net.calls[1], Object.assign({}, content, { asOf: 60, tiles: { r: 1, y: 1, g: 0, active: null }, count: '2 von 9 Begegnungen' }));
  now = unit();
  assert.notEqual(now.count, was.count);
  assert.notEqual(t.q('#ltd-count'), countHeading);
  assert.equal(t.text('#ltd-count'), '2 von 9 Begegnungen');
  assert.equal(now.filters, was.filters);

  // A filter chip changed: the filters are new.
  const afterCount = unit();
  await t.tick(30000);
  await t.answer(t.net.calls[2], Object.assign({}, content, { asOf: 90, tiles: { r: 1, y: 1, g: 0, active: null }, count: '2 von 9 Begegnungen', chips: ['Stufe: Gelb', 'Sortierung: Wartezeit'] }));
  now = unit();
  assert.notEqual(now.filters, afterCount.filters);
  assert.equal(t.qa('form.ltd-filters .ltd-chip').length, 2);
  assert.equal(now.pager, afterCount.pager);
  assert.equal(now.count, afterCount.count);

  // A page was added: the paging is new; and it can go away again.
  const afterChip = unit();
  await t.tick(30000);
  const full = { tiles: { r: 1, y: 1, g: 0, active: null }, count: '2 von 9 Begegnungen', chips: ['Stufe: Gelb', 'Sortierung: Wartezeit'] };
  await t.answer(t.net.calls[3], Object.assign({ rows, asOf: 120, pager: [1, 2, 3] }, full));
  now = unit();
  assert.notEqual(now.pager, afterChip.pager);
  assert.equal(t.qa('nav.ltd-pager a').length, 3);
  assert.equal(now.filters, afterChip.filters);
  await t.tick(30000);
  await t.answer(t.net.calls[4], Object.assign({ rows, asOf: 150 }, full));
  assert.equal(t.q('nav.ltd-pager'), null);
  assert.ok(same(t.qa('li.ltd-row'), rowNodes), 'the rows stayed through all of it');
});

test('test_the_json_stale_ack_detail_is_shown_and_other_codes_have_none', async () => {
  const message = 'Der Stand hat sich geändert. Bitte aktualisieren und erneut prüfen.';
  const detail = 'Mara hat diese Verzögerung um 14:15 bereits festgehalten. Dein Entwurf ist unten erhalten und wurde nicht gesendet.';
  const refuse = async (status, body, rows = [row(1), row(2)]) => {
    const t = setup({ panel: { rows: [row(1), row(2)] } });
    const form = t.open(1);
    t.type(form.querySelector('textarea'), 'Entwurf');
    t.click(t.q('#form-1 button[type="submit"]'));
    await flush();
    t.net.posts()[0].respond(status, Object.assign({ fragment: t.fragment({ asOf: 20, rows }) }, body));
    await flush();
    return t;
  };

  // The row is still there and offers the check: the detail sits under the message, in the form.
  const target = await refuse(409, { error: 'stale', message, draft_target: true, detail });
  assert.equal(target.text('#form-1 .ltd-fmsg b'), message);
  assert.equal(target.text('#form-1 .ltd-fmsg p'), detail);
  assert.equal(target.q('#form-1 .ltd-fmsg').getAttribute('role'), 'alert');
  assert.equal(target.q('#c-1').value, 'Entwurf');

  // The row left the list: the same two lines, in the notice.
  const gone = await refuse(409, { error: 'stale', message, draft_target: false, detail }, [row(2)]);
  assert.equal(gone.text('.ltd-ban.b-err .ltd-banh'), `!${message}`);
  assert.equal(gone.text('.ltd-ban.b-err .ltd-banb'), detail);
  assert.equal(gone.q('.ltd-draft textarea').value, 'Entwurf');

  // The answer is older than the panel: the form still says whom to thank.
  const older = setup({ panel: { rows: [row(1), row(2)] } });
  const fo = older.open(1);
  older.type(fo.querySelector('textarea'), 'Entwurf');
  older.click(older.q('#form-1 button[type="submit"]'));
  await flush();
  older.net.posts()[0].respond(409, { error: 'stale', message, draft_target: true, detail, fragment: older.fragment({ asOf: -100 }) });
  await flush();
  assert.equal(older.text('#form-1 .ltd-fmsg b'), message);
  assert.equal(older.text('#form-1 .ltd-fmsg p'), detail);

  // An answer without a panel: the form still gets both lines.
  const lonely = await refuse(409, { error: 'stale', message, draft_target: true, detail, fragment: undefined });
  assert.equal(lonely.text('#form-1 .ltd-fmsg b'), message);
  assert.equal(lonely.text('#form-1 .ltd-fmsg p'), detail);

  // No detail from the server (the row is hidden or gone for this viewer): none shown.
  const bare = await refuse(409, { error: 'stale', message, draft_target: true });
  assert.equal(bare.text('#form-1 .ltd-fmsg b'), message);
  assert.equal(bare.q('#form-1 .ltd-fmsg p'), null);
  const empty = await refuse(409, { error: 'stale', message, draft_target: true, detail: '' });
  assert.equal(empty.q('#form-1 .ltd-fmsg p'), null);
  for (const odd of [42, true, { text: detail }, [detail]]) {
    const other = await refuse(409, { error: 'stale', message, draft_target: true, detail: odd });
    assert.equal(other.q('#form-1 .ltd-fmsg p'), null, JSON.stringify(odd));
  }

  // Other codes show their own lines, whatever a `detail` field says.
  const refused = await refuse(409, { error: 'refused', message: 'Pausiert.', draft_target: true, detail });
  assert.equal(refused.text('#form-1 .ltd-fmsg b'), 'Nicht festgehalten: Pausiert.');
  assert.equal(refused.text('#form-1 .ltd-fmsg p'), LABELS.ack_refused_detail, 'the label, not the field');
  const invalid = await refuse(422, { error: 'invalid', message: 'Kommentar ist zu lang.', draft_target: true, detail });
  assert.equal(invalid.text('#form-1 .ltd-fmsg b'), 'Kommentar ist zu lang.');
  assert.equal(invalid.q('#form-1 .ltd-fmsg p'), null);
  [refused, invalid].forEach((t) => assert.ok(!t.root.textContent.includes(detail), 'the field was not shown anywhere'));

  // A stale pin has no detail either.
  const pin = setup();
  pin.click(pin.q('#lt-row-2 button[data-pin]'));
  await flush();
  pin.net.posts()[0].respond(409, { error: 'stale', message, draft_target: true, detail, fragment: pin.fragment({ asOf: 40 }) });
  await flush();
  assert.equal(pin.text('#lt-row-2 .ltd-err'), message);
  assert.ok(!pin.root.textContent.includes(detail));

  // The detail is text, never markup.
  const hostile = await refuse(409, { error: 'stale', message, draft_target: true, detail: '<img src=x onerror=alert(1)>' });
  assert.equal(hostile.text('#form-1 .ltd-fmsg p'), '<img src=x onerror=alert(1)>');
  assert.equal(hostile.qa('img').length, 0);
});

test('test_the_row_that_was_acted_on_starts_again_from_the_server_markup', async () => {
  // The server's row is the same as the one on screen, so a plain diff would
  // keep it: its form would stay pending. The acted row is rebuilt.
  const t = setup({ panel: { rows: [row(1), row(2)] } });
  const form = t.open(1);
  t.type(form.querySelector('textarea'), 'Entwurf');
  t.click(t.q('#form-1 button[type="submit"]'));
  await flush();
  assert.equal(t.q('#form-1 button[type="submit"]').hasAttribute('disabled'), true);
  const neighbour = t.q('#lt-row-2');
  t.net.posts()[0].respond(422, { error: 'invalid', message: 'Kommentar ist zu lang.', draft_target: true, fragment: t.fragment({ asOf: 20 }) });
  await flush();
  assert.notEqual(t.q('#form-1'), form, 'a new form');
  assert.equal(t.q('#form-1 button[type="submit"]').hasAttribute('disabled'), false);
  assert.equal(t.q('#form-1 button[type="submit"]').textContent, 'Prüfung festhalten');
  assert.equal(t.q('#form-1 .ltd-pend'), null);
  assert.equal(t.q('#form-1').hasAttribute('data-open'), true);
  assert.equal(t.q('#c-1').value, 'Entwurf');
  assert.equal(t.q('#c-1').hasAttribute('readonly'), false);
  assert.equal(t.q('#lt-row-2'), neighbour, 'the other rows are not rebuilt');

  // A pin the server found unchanged is not left pending either.
  const p = setup();
  p.click(p.q('#lt-row-2 button[data-pin]'));
  await flush();
  assert.equal(p.q('#lt-row-2 button[data-pin]').hasAttribute('disabled'), true);
  p.net.posts()[0].respond(200, { committed_at: iso(30), fragment: p.fragment({ asOf: 30 }) });
  await flush();
  assert.equal(p.q('#lt-row-2 button[data-pin]').hasAttribute('disabled'), false);
  assert.equal(p.q('#lt-row-2 button[data-pin]').textContent, 'Für das Orga-Team anpinnen');
  assert.equal(p.controller.state().hold, null);
});

test('test_the_scripts_own_notices_do_not_outlive_the_next_swap_on_a_kept_panel', async () => {
  const t = setup();
  const row2 = t.q('#lt-row-2');
  t.click(t.q('#lt-row-2 button[data-pin]'));
  await flush();
  t.net.posts()[0].fail();
  await flush();
  assert.equal(t.text('#lt-row-2 .ltd-err'), LABELS.pin_failed);
  t.q('#lt-row-2 .ltd-mlink').focus();

  // Releasing the hold fetched once; the answer finds nothing new: the
  // row stays, the notice does not.
  assert.equal(t.net.gets().length, 1);
  await t.answer(t.net.gets()[0], { asOf: 30 });
  assert.equal(t.q('#lt-row-2'), row2);
  assert.equal(t.q('#lt-row-2 .ltd-err'), null);
  assert.equal(t.qa('[data-ltd-js]').length, 0);
  assert.equal(t.q('#lt-row-2 button[data-pin]').hasAttribute('disabled'), false);
});

test('test_every_reordering_of_the_rows_ends_in_server_order_with_the_fewest_moves', async () => {
  const permutations = (list) =>
    list.length <= 1 ? [list] : list.flatMap((x, i) => permutations(list.filter((_, j) => j !== i)).map((rest) => [x, ...rest]));
  // The rows that may stay are the longest run already in the wanted order.
  const longestRun = (order) => {
    const best = order.map(() => 1);
    order.forEach((value, i) => {
      for (let j = 0; j < i; j++) if (order[j] < value) best[i] = Math.max(best[i], best[j] + 1);
    });
    return Math.max(...best);
  };
  for (const order of permutations([1, 2, 3, 4])) {
    const label = order.join(',');
    const t = setup({ panel: { rows: [1, 2, 3, 4].map((n) => row(n)) } });
    const nodes = Object.fromEntries(t.qa('li.ltd-row').map((node) => [node.id, node]));
    const list = t.q('ol.ltd-rows');
    await t.tick(30000);
    const watch = watchStructure(t.root);
    try {
      await t.answer(t.net.calls[0], { asOf: 30, rows: order.map((n) => row(n)) });
    } finally {
      watch.stop();
    }
    assert.deepEqual(rowIds(t), order.map((n) => `lt-row-${n}`), label);
    order.forEach((n) => assert.equal(t.q(`#lt-row-${n}`), nodes[`lt-row-${n}`], `${label}: row ${n} is the same node`));
    const moves = watch.seen.filter((c) => c.name === 'insertBefore' && c.target === list).length;
    assert.equal(moves, 4 - longestRun(order), `${label}: only the rows that had to move were moved`);
  }
});

test('test_a_notice_that_appears_and_goes_leaves_the_rest_of_the_panel_alone', async () => {
  const content = { rows: [row(1), row(2)] };
  const t = setup({ panel: content });
  const topLevel = () => t.root.childNodes.filter((n) => n.nodeType === 1);
  const classes = () => topLevel().map((n) => n.getAttribute('class').split(' ')[0]);
  const before = topLevel();
  assert.deepEqual(classes(), ['ltd-top', 'ltd-sum', 'ltd-filters', 'ltd-count', 'ltd-rows', 'ltd-sr']);

  // The server adds a notice between the filters and the count.
  await t.tick(30000);
  await t.answer(t.net.calls[0], Object.assign({ asOf: 30, banner: 'Filter gekürzt: 2 Turniere nicht sichtbar.' }, content));
  assert.deepEqual(classes(), ['ltd-top', 'ltd-sum', 'ltd-filters', 'ltd-ban', 'ltd-count', 'ltd-rows', 'ltd-sr']);
  assert.equal(t.text('.ltd-ban .ltd-banh'), 'iFilter gekürzt: 2 Turniere nicht sichtbar.');
  const kept = topLevel().filter((n) => before.includes(n));
  assert.equal(kept.length, before.length, 'every unit that was there is the same node');
  assert.equal(t.q('.ltd-ban').getAttribute('data-ltd-js'), null, 'the server\'s notice, not the script\'s');

  // It goes again, and the notice is gone while the rest stays.
  await t.tick(30000);
  await t.answer(t.net.calls[1], Object.assign({ asOf: 60 }, content));
  assert.deepEqual(classes(), ['ltd-top', 'ltd-sum', 'ltd-filters', 'ltd-count', 'ltd-rows', 'ltd-sr']);
  assert.ok(same(topLevel(), before));
});
