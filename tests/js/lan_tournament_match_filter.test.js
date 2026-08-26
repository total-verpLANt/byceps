'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const SOURCE = fs.readFileSync(path.join(
  __dirname, '../../byceps/static/behavior/lan_tournament.js'
), 'utf8');

const BASE = 'https://party.example/lan-tournaments/t1/matches';
const LINKS = [
  '/lan-tournaments/t1/matches?only=waiting',
  '/lan-tournaments/t1/matches?only=both_ready&view=compact',
  '/lan-tournaments/t1/matches?only=all'
];

function element(attributes = {}) {
  const classes = new Set();
  return {
    attributes: { ...attributes },
    getAttribute(key) { return key in this.attributes ? this.attributes[key] : null; },
    setAttribute(key, value) { this.attributes[key] = String(value); },
    addEventListener(type, fn) { this.listener = fn; },
    classList: {
      add(name) { classes.add(name); },
      remove(name) { classes.delete(name); },
      contains(name) { return classes.has(name); },
      toggle(name, force) {
        if (force === undefined ? !classes.has(name) : force) classes.add(name);
        else classes.delete(name);
      }
    }
  };
}

function load({ search = '', withButton = true } = {}) {
  const button = element({
    id: 'match-filter-mine',
    'data-user-team-id': 'team-1',
    'data-user-participant-id': 'participant-1'
  });
  const links = LINKS.map((href) => element({ href }));
  const mine = element({ 'data-team-ids': 'team-1,team-2', 'data-participant-ids': ',' });
  const other = element({ 'data-team-ids': 'team-3,team-4', 'data-participant-ids': ',' });
  const location = { href: BASE + search, search };
  const replaced = [];
  const sandbox = {
    URL,
    URLSearchParams,
    location,
    document: {
      addEventListener() {},
      getElementById(id) { return withButton && id === 'match-filter-mine' ? button : null; },
      querySelectorAll(selector) {
        if (selector === '.match-filter-bar__item') return links;
        if (selector === '.match-link-reset') return [mine, other];
        return [];
      }
    },
    history: {
      replaceState(state, title, url) {
        const target = new URL(url);
        location.href = target.href;
        location.search = target.search;
        replaced.push(target.search);
      }
    }
  };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(SOURCE, sandbox);
  sandbox.initMatchFilter();
  return { button, links, mine, other, location, replaced };
}

function query(link) {
  return new URL(link.getAttribute('href'), BASE).searchParams;
}

test('toggle on adds my_matches to every filter link and keeps the other params', () => {
  const page = load();
  assert.deepEqual(page.links.map((link) => link.getAttribute('href')), LINKS);

  page.button.listener();

  assert.equal(page.button.getAttribute('aria-pressed'), 'true');
  for (const [index, link] of page.links.entries()) {
    const params = query(link);
    assert.equal(params.get('my_matches'), '1');
    assert.equal(params.getAll('my_matches').length, 1);
    assert.equal(params.get('only'), new URL(LINKS[index], BASE).searchParams.get('only'));
  }
  assert.equal(query(page.links[1]).get('view'), 'compact');
  assert.ok(page.links[0].getAttribute('href').startsWith('/lan-tournaments/t1/matches?'));
  assert.equal(page.location.search, '?my_matches=1');
});

test('toggle off removes my_matches from every filter link again', () => {
  const page = load();
  page.button.listener();
  page.button.listener();

  assert.equal(page.button.getAttribute('aria-pressed'), 'false');
  assert.deepEqual(page.links.map((link) => link.getAttribute('href')), LINKS);
  assert.deepEqual(page.replaced, ['?my_matches=1', '']);
});

test('state restored from the URL reaches the links on load', () => {
  const page = load({ search: '?only=both_ready&my_matches=1' });

  assert.equal(page.button.getAttribute('aria-pressed'), 'true');
  assert.equal(page.mine.classList.contains('match-row--hidden'), false);
  assert.ok(page.other.classList.contains('match-row--hidden'));
  for (const link of page.links) {
    assert.equal(query(link).get('my_matches'), '1');
  }
  assert.equal(query(page.links[0]).get('only'), 'waiting');
});

test('an inactive page leaves the server-rendered links untouched', () => {
  const page = load({ search: '?only=waiting' });

  assert.equal(page.button.getAttribute('aria-pressed'), null);
  assert.deepEqual(page.links.map((link) => link.getAttribute('href')), LINKS);
  assert.deepEqual(page.replaced, []);
});

test('a page without the toggle keeps its links', () => {
  const page = load({ search: '?my_matches=1', withButton: false });

  assert.deepEqual(page.links.map((link) => link.getAttribute('href')), LINKS);
});
