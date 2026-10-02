'use strict';

const test = require('node:test');
const assert = require('node:assert');

const { initBoard } = require('../../byceps/static/behavior/lan_tournament_seeding.js');

// A minimal DOM: tag, `.class`, `[attr]` and `[attr="value"]` compounds,
// joined by descendant combinators and commas.

function parseCompound(text) {
  const parts = { tag: null, classes: [], attrs: [] };
  const re = /^[a-z]+|\.[\w-]+|\[([\w-]+)(?:="([^"]*)")?\]/gi;
  let m;
  while ((m = re.exec(text)) !== null) {
    if (m[0][0] === '.') parts.classes.push(m[0].slice(1));
    else if (m[0][0] === '[') parts.attrs.push([m[1], m[2]]);
    else parts.tag = m[0].toUpperCase();
  }
  return parts;
}

function matchesCompound(node, parts) {
  if (parts.tag !== null && node.tagName !== parts.tag) return false;
  if (!parts.classes.every((c) => node.classList.contains(c))) return false;
  return parts.attrs.every(([name, value]) => {
    const actual = node.getAttribute(name);
    return value === undefined ? actual !== null : actual === value;
  });
}

function matchesSelector(node, selector) {
  return selector.split(',').some((alternative) => {
    const chain = alternative.trim().split(/\s+/).map(parseCompound);
    if (!matchesCompound(node, chain[chain.length - 1])) return false;
    let k = chain.length - 2;
    for (let up = node.parentNode; up !== null && k >= 0; up = up.parentNode) {
      if (up.tagName && matchesCompound(up, chain[k])) k -= 1;
    }
    return k < 0;
  });
}

class FakeElement {
  constructor(tag, attrs) {
    this.tagName = String(tag).toUpperCase();
    this.attrs = {};
    this.children = [];
    this.parentNode = null;
    this.listeners = {};
    this.hidden = false;
    this.type = '';
    this._text = '';
    Object.entries(attrs || {}).forEach(([k, v]) => this.setAttribute(k, v));
    const classes = () => (this.attrs.class || '').split(/\s+/).filter(Boolean);
    const write = (list) => this.setAttribute('class', list.join(' '));
    this.classList = {
      add: (c) => classes().includes(c) || write(classes().concat(c)),
      remove: (c) => write(classes().filter((x) => x !== c)),
      toggle: (c, on) => ((on === undefined ? !classes().includes(c) : on)
        ? this.classList.add(c) : this.classList.remove(c)),
      contains: (c) => classes().includes(c)
    };
  }
  get className() { return this.getAttribute('class') || ''; }
  set className(v) { this.setAttribute('class', v); }
  get textContent() { return this._text + this.children.map((c) => c.textContent).join(''); }
  set textContent(v) { this._text = String(v); this.children = []; }
  get firstChild() { return this.children[0] || null; }
  get nextSibling() {
    if (this.parentNode === null) return null;
    const siblings = this.parentNode.children;
    return siblings[siblings.indexOf(this) + 1] || null;
  }
  set innerHTML(v) { this.onInnerHTML(v); }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  hasAttribute(k) { return k in this.attrs; }
  removeAttribute(k) { delete this.attrs[k]; }
  appendChild(node) { return this.insertBefore(node, null); }
  insertBefore(node, ref) {
    if (node.parentNode) node.parentNode.removeChild(node);
    const at = ref === null ? -1 : this.children.indexOf(ref);
    if (at < 0) this.children.push(node); else this.children.splice(at, 0, node);
    node.parentNode = this;
    return node;
  }
  removeChild(node) {
    this.children.splice(this.children.indexOf(node), 1);
    node.parentNode = null;
    return node;
  }
  contains(node) {
    for (let n = node; n !== null; n = n.parentNode) if (n === this) return true;
    return false;
  }
  matches(selector) { return matchesSelector(this, selector); }
  closest(selector) {
    for (let n = this; n !== null && n.tagName; n = n.parentNode) {
      if (n.matches(selector)) return n;
    }
    return null;
  }
  querySelectorAll(selector) {
    const found = [];
    const walk = (node) => node.children.forEach((child) => {
      if (child.tagName && child.matches(selector)) found.push(child);
      walk(child);
    });
    walk(this);
    return found;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  removeEventListener() {}
  focus() { global.document.activeElement = this; }
}

const STRINGS = {
  bye: 'Bye',
  swapped: 'Swapped: %(a)s (%(wa)s) with %(b)s (%(wb)s).',
  where_match: 'M%(n)s, position %(pos)s',
  hint: 'Choose where %(name)s goes.',
  selected: '%(name)s selected.',
  saved_reload: 'Saved. Reload the page to see the board.',
  reload: 'Reload',
  failed: 'Not saved.'
};

function board(version, names) {
  const slot = (index) => ({
    index: index,
    contestant_id: 'id-' + names[index],
    name: names[index],
    bye: false,
    seed_position: index + 1,
    group: null
  });
  return {
    target: 'initial',
    version: version,
    format: 'SE',
    generation: 'open',
    fix_count: 0,
    layout: {
      kind: 'bracket',
      matches: [
        { number: 1, slots: [slot(0), slot(1)] },
        { number: 2, slots: [slot(2), slot(3)] }
      ],
      groups: []
    },
    strings: STRINGS
  };
}

// The board markup as the server renders it, reduced to what the script reads.
function fillRoot(rootEl, data) {
  rootEl.children = [];
  const script = new FakeElement('script', { 'data-lt-seed-board': '' });
  script.textContent = JSON.stringify(data);
  rootEl.appendChild(script);
  rootEl.appendChild(new FakeElement('div', { 'data-lt-seed-save': '' }));
  rootEl.appendChild(new FakeElement('div', { 'data-lt-seed-banners': '' }));
  const list = new FakeElement('div', { 'data-swap-list': '' });
  data.layout.matches.forEach((match) => match.slots.forEach((s) => {
    const chip = new FakeElement('div', { class: 'lt-seed-chip', 'data-i': s.index });
    const button = new FakeElement('button', { class: 'lt-seed-cb' });
    button.textContent = s.name;
    chip.appendChild(button);
    list.appendChild(chip);
  }));
  rootEl.appendChild(list);
}

function harness(pages) {
  const posts = [];
  const log = { reloads: 0 };
  const body = new FakeElement('body');
  global.document = {
    body: body,
    activeElement: null,
    createElement: (tag) => new FakeElement(tag),
    createTextNode: (text) => {
      const node = new FakeElement('#text');
      node.tagName = null;
      node.textContent = text;
      return node;
    },
    querySelectorAll: (selector) => body.querySelectorAll(selector),
    addEventListener() {},
    removeEventListener() {}
  };
  global.window = {
    addEventListener() {},
    location: { reload: () => { log.reloads += 1; } }
  };
  global.FormData = class {
    constructor(form) { this.fields = form.fields; }
    forEach(fn) { Object.entries(this.fields).forEach(([k, v]) => fn(v, k)); }
  };
  global.DOMParser = class {
    parseFromString(text) {
      return { querySelector: () => ({ innerHTML: text }) };
    }
  };
  const rootEl = new FakeElement('div', {
    'data-lt-seed-root': '',
    'data-action-url': '/act',
    'data-page-url': '/page'
  });
  rootEl.onInnerHTML = (text) => fillRoot(rootEl, JSON.parse(text));
  body.appendChild(rootEl);
  global.fetch = (url, init) => {
    if (url === '/act') {
      const fields = new URLSearchParams(init.body);
      posts.push(Object.fromEntries(fields));
      const next = board(Number(fields.get('version')) + 1, ['B', 'A', 'C', 'D']);
      return Promise.resolve({
        status: 200,
        json: () => Promise.resolve({ board: next })
      });
    }
    const page = pages.shift();
    if (page === undefined) return Promise.reject(new TypeError('offline'));
    return Promise.resolve({ text: () => Promise.resolve(JSON.stringify(page)) });
  };
  fillRoot(rootEl, board(1, ['A', 'B', 'C', 'D']));
  initBoard(rootEl);
  return {
    rootEl: rootEl,
    posts: posts,
    log: log,
    tapSlot(index) {
      const button = rootEl.querySelector('.lt-seed-chip[data-i="' + index + '"] .lt-seed-cb');
      rootEl.listeners.click.forEach((fn) => fn({ target: button }));
    },
    tap(button) {
      rootEl.listeners.click.forEach((fn) => fn({ target: button }));
    },
    submit(fields) {
      const form = new FakeElement('form', { action: '/act' });
      form.fields = fields;
      rootEl.appendChild(form);
      rootEl.listeners.submit.forEach((fn) => fn({
        target: form,
        submitter: null,
        preventDefault() {}
      }));
    }
  };
}

async function settle() {
  for (let i = 0; i < 20; i++) await new Promise((resolve) => setImmediate(resolve));
}

test('a save whose refresh fails keeps the board from taking another action', async () => {
  const h = harness([]);
  h.tapSlot(0);
  h.tapSlot(1);
  await settle();
  assert.strictEqual(h.posts.length, 1);

  // The page still shows A B C D; the server now holds B A C D.
  h.tapSlot(0);
  h.tapSlot(2);
  await settle();

  assert.strictEqual(h.posts.length, 1, 'a swap was sent: ' + JSON.stringify(h.posts[1]));
  assert.strictEqual(h.rootEl.querySelector('.lt-seed-hintbar'), null);
});

test('a save whose refresh fails keeps the board forms from posting', async () => {
  const h = harness([]);
  h.tapSlot(0);
  h.tapSlot(1);
  await settle();

  h.submit({ action: 'separate' });
  await settle();

  assert.strictEqual(h.posts.length, 1, 'a form was sent: ' + JSON.stringify(h.posts[1]));
});

test('a save whose refresh fails offers a reload', async () => {
  const h = harness([]);
  h.tapSlot(0);
  h.tapSlot(1);
  await settle();

  const reload = h.rootEl.querySelector('[data-lt-seed-banners] [data-reload]');
  assert.notStrictEqual(reload, null);
  h.tap(reload);
  assert.strictEqual(h.log.reloads, 1);
});

test('a save whose refresh succeeds sends the next action with the new version', async () => {
  const h = harness([board(2, ['B', 'A', 'C', 'D'])]);
  h.tapSlot(0);
  h.tapSlot(1);
  await settle();

  h.tapSlot(0);
  h.tapSlot(2);
  await settle();

  assert.strictEqual(h.posts.length, 2);
  assert.deepStrictEqual(
    h.posts.map((post) => [post.version, post.p, post.q]),
    [['1', '0', '1'], ['2', '0', '2']]
  );
});
