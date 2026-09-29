'use strict';

// Drives the real image module against hand-made DOM, XHR and fetch stubs
// (the repo ships no jsdom). Only the ordering of upload, precheck and DELETE
// calls is asserted.

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const SOURCE = fs.readFileSync(
  path.join(
    __dirname, '../../byceps/static/behavior/lan_tournament_create_wizard_image.js'
  ),
  'utf8'
);

const ID_A = '0190aaaa-bbbb-7ccc-8ddd-eeeeeeeeeeea';
const ID_B = '0190aaaa-bbbb-7ccc-8ddd-eeeeeeeeeeeb';
const MAX_BYTES = 5 * 1024 * 1024;

class FakeEvent {
  constructor(type, init) {
    this.type = type;
    this.detail = init && init.detail;
  }
  preventDefault() {}
  stopPropagation() {}
}

class FakeNode {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.attrs = {};
    this.children = [];
    this.listeners = {};
    this.style = {};
    this.value = '';
    this.hidden = false;
    this.disabled = false;
    this.checked = false;
    this.id = '';
    this.parentNode = null;
    this._text = '';
    const set = new Set();
    this.classList = {
      add: (c) => set.add(c),
      remove: (c) => set.delete(c),
      toggle: (c, on) => (on === undefined ? !set.has(c) : on) ? set.add(c) : set.delete(c),
      contains: (c) => set.has(c)
    };
  }
  set className(v) { v.split(' ').forEach((c) => c && this.classList.add(c)); }
  get textContent() { return this._text; }
  set textContent(v) { this._text = String(v); this.children = []; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  removeAttribute(k) { delete this.attrs[k]; }
  appendChild(n) { this.insertBefore(n, null); return n; }
  insertBefore(n, ref) {
    if (n.parentNode) {
      n.parentNode.children = n.parentNode.children.filter((c) => c !== n);
    }
    n.parentNode = this;
    const at = ref ? this.children.indexOf(ref) : -1;
    if (at < 0) { this.children.push(n); } else { this.children.splice(at, 0, n); }
    return n;
  }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  dispatchEvent(ev) { (this.listeners[ev.type] || []).forEach((fn) => fn(ev)); }
  click() { this.dispatchEvent(new FakeEvent('click')); }
  focus() {}
  querySelector(sel) { return (this.found && this.found[sel]) || null; }
  descendants() {
    return this.children.reduce((all, c) => all.concat(c, c.descendants()), []);
  }
}

function makeEnv() {
  const calls = [];
  const xhrs = [];
  const events = [];
  const docListeners = {};
  const doc = new FakeNode('#document');
  const config = {
    urls: {upload: '/up', images: '/imgs', delete_template: '/del/__ID__'},
    limits: {uploadBytes: MAX_BYTES}, strings: {}, locale: 'en'
  };
  const form = new FakeNode('form');
  const hidden = new FakeNode('input');
  const root = new FakeNode('div');
  const fileInput = new FakeNode('input');
  const dropEl = new FakeNode('div');
  const libOpen = new FakeNode('button');
  const errServer = new FakeNode('div');
  const altWrap = new FakeNode('div');
  const altInput = new FakeNode('input');
  const urlAlt = new FakeNode('details');
  const island = {textContent: JSON.stringify(config)};
  fileInput.id = 'image';
  hidden.value = '';
  root.appendChild(dropEl);
  root.appendChild(urlAlt);
  root.appendChild(altWrap);
  altWrap.appendChild(altInput);
  Object.assign(form.found = {}, {'input[name="image_id"]': hidden});
  Object.assign(root.found = {}, {
    'input[data-wiz-file]': fileInput,
    '[data-wiz-drop]': dropEl,
    '[data-wiz-lib-open]': libOpen,
    '[data-wiz-err-for="image"]': errServer,
    '[data-wiz-field="image_alt_text"]': altWrap,
    'input[name="image_alt_text"]': altInput,
    'details.lt-wiz-urlalt': urlAlt
  });
  Object.assign(doc, {
    readyState: 'complete',
    activeElement: null,
    documentElement: {lang: 'en'},
    createElement: (tag) => new FakeNode(tag),
    createTextNode: (text) => Object.assign(new FakeNode('#text'), {_text: text}),
    getElementById: (id) => (id === 'lt-create-wizard-config' ? island : null),
    addEventListener: (type, fn) => (docListeners[type] = docListeners[type] || []).push(fn),
    dispatchEvent: (ev) => {
      events.push(ev);
      (docListeners[ev.type] || []).forEach((fn) => fn(ev));
    }
  });
  doc.querySelector = (sel) => ({
    '[data-wiz-image]': root,
    'form[data-lt-wizard]': form,
    '[data-wiz-announce]': null
  })[sel] || null;
  form.found['input[name="image_id"]'] = hidden;

  class FakeXHR {
    constructor() { this.upload = {}; this.aborted = false; xhrs.push(this); }
    open(method, url) { this.method = method; this.url = url; }
    setRequestHeader() {}
    send() { calls.push('XHR ' + this.method + ' ' + this.url); }
    abort() { this.aborted = true; }
    respond(status, body) {
      this.status = status;
      this.responseText = JSON.stringify(body);
      this.onload();
    }
  }

  const items = [];
  const fetchStub = (url, opts) => {
    const method = (opts && opts.method) || 'GET';
    calls.push(method + ' ' + url);
    if (String(url).startsWith('/imgs')) {
      const body = JSON.stringify({items, page: 1, has_next: false});
      return Promise.resolve({ok: true, text: () => Promise.resolve(body)});
    }
    return Promise.resolve({ok: true, status: 204});
  };

  const sandbox = {
    document: doc, location: new URL('https://admin.example/create'),
    URL, Intl, JSON, Math, Object, Array, String, Number, Error, isFinite,
    Event: FakeEvent, CustomEvent: FakeEvent, FormData: class { append() {} },
    XMLHttpRequest: FakeXHR, fetch: fetchStub, setTimeout, clearTimeout
  };
  sandbox.window = sandbox;
  vm.runInNewContext(SOURCE, sandbox);

  const upload = (name, size) => {
    fileInput.files = [{name, size: size || 10, type: 'image/png'}];
    fileInput.dispatchEvent(new FakeEvent('change'));
  };
  const okBody = (id) => ({
    image_id: id, url: '/data/' + id + '.png', filename: id + '.png',
    width: 1920, height: 1080, byte_size: 1000
  });
  const tick = () => new Promise((resolve) => setTimeout(resolve, 5));
  const deletes = () => calls.filter((c) => c.startsWith('DELETE'));
  const lastState = () => events[events.length - 1].detail;

  async function pickFromLibrary(id) {
    items.length = 0;
    items.push({
      image_id: id, url: '/data/' + id + '.png', filename: id + '.png',
      width: 1920, height: 1080, byte_size: 1000, used_by: []
    });
    libOpen.click();
    await tick();
    const panel = root.children.find((c) => c.attrs['data-wiz-lib'] !== undefined);
    const radio = panel.descendants().find((n) => n.name === 'wiz-lib');
    radio.checked = true;
    radio.dispatchEvent(new FakeEvent('change'));
    const use = panel.descendants().find((n) => n.tagName === 'BUTTON'
      && n.listeners.click && n.textContent === 'Use this image');
    use.click();
    await tick();
    return panel;
  }

  const actionButton = () => root.children
    .find((c) => c.attrs['data-wiz-image-live'] !== undefined)
    .descendants().find((n) => n.tagName === 'BUTTON' && n.listeners.click
      && ['Remove', 'Cancel upload', 'Continue without image'].includes(n.textContent));

  return {
    calls, xhrs, hidden, errServer, upload, okBody, tick, deletes, lastState,
    pickFromLibrary, actionButton
  };
}

async function stageUpload(env, id) {
  env.upload('a.png');
  env.xhrs[env.xhrs.length - 1].respond(201, env.okBody(id));
  await env.tick();
  assert.strictEqual(env.hidden.value, id);
}

test('re-picking the staged image from the library does not delete it', async () => {
  const env = makeEnv();
  await stageUpload(env, ID_A);
  await env.pickFromLibrary(ID_A);
  assert.deepStrictEqual(env.deletes(), []);
  assert.strictEqual(env.hidden.value, ID_A);
});

test('picking another library image releases the uploaded one afterwards', async () => {
  const env = makeEnv();
  await stageUpload(env, ID_A);
  await env.pickFromLibrary(ID_B);
  assert.deepStrictEqual(env.deletes(), ['DELETE /del/' + ID_A]);
  assert.strictEqual(env.hidden.value, ID_B);
});

test('a replacement rejected by the precheck keeps the staged image', async () => {
  const env = makeEnv();
  await stageUpload(env, ID_A);
  env.upload('big.png', MAX_BYTES + 1);
  assert.deepStrictEqual(env.deletes(), []);
  assert.strictEqual(env.hidden.value, ID_A);
  assert.notStrictEqual(env.errServer.textContent + env.errServer.children.length, '0');
});

test('the previous upload is released only after the replacement succeeded', async () => {
  const env = makeEnv();
  await stageUpload(env, ID_A);
  env.upload('b.png');
  assert.deepStrictEqual(env.deletes(), []);
  env.xhrs[env.xhrs.length - 1].respond(201, env.okBody(ID_B));
  await env.tick();
  assert.deepStrictEqual(env.deletes(), ['DELETE /del/' + ID_A]);
  assert.strictEqual(env.hidden.value, ID_B);
});

test('a replacement refused by the server keeps the staged image', async () => {
  const env = makeEnv();
  await stageUpload(env, ID_A);
  env.upload('b.png');
  env.xhrs[env.xhrs.length - 1].respond(422, {error: 'too small'});
  await env.tick();
  assert.deepStrictEqual(env.deletes(), []);
  assert.strictEqual(env.hidden.value, ID_A);
  assert.strictEqual(env.lastState().status, 'done');
  assert.strictEqual(env.lastState().imageId, ID_A);
});

test('cancelling an in-flight replacement restores the staged image', async () => {
  const env = makeEnv();
  await stageUpload(env, ID_A);
  env.upload('b.png');
  const xhr = env.xhrs[env.xhrs.length - 1];
  assert.strictEqual(env.hidden.value, '');
  env.actionButton().click();
  assert.ok(xhr.aborted);
  assert.deepStrictEqual(env.deletes(), []);
  assert.strictEqual(env.hidden.value, ID_A);
  assert.strictEqual(env.lastState().status, 'done');
});

test('a second pick during an upload still remembers the first image', async () => {
  const env = makeEnv();
  await stageUpload(env, ID_A);
  env.upload('b.png');
  env.upload('c.png');
  env.xhrs[env.xhrs.length - 1].respond(201, env.okBody(ID_B));
  await env.tick();
  assert.deepStrictEqual(env.deletes(), ['DELETE /del/' + ID_A]);
  assert.strictEqual(env.hidden.value, ID_B);
});

test('Remove still releases the uploaded image', async () => {
  const env = makeEnv();
  await stageUpload(env, ID_A);
  env.actionButton().click();
  assert.deepStrictEqual(env.deletes(), ['DELETE /del/' + ID_A]);
  assert.strictEqual(env.hidden.value, '');
});
