'use strict';

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const SOURCE = fs.readFileSync(
  path.join(
    __dirname,
    '../../byceps/static/behavior/lan_tournament_bracket.js'
  ),
  'utf8'
);

function load() {
  const sandbox = {
    document: { addEventListener() {}, getElementById() { return null; } },
    window: { addEventListener() {} }
  };
  vm.createContext(sandbox);
  vm.runInContext(SOURCE, sandbox);
  return sandbox;
}

const DIMS = { teamHeight: 28, fontScale: 1 };

function row(sandbox, contestant, hoverData) {
  const entrant = sandbox._ltBuildEntrant(contestant, null);
  return sandbox.buildTeamRow(
    entrant, false, null, DIMS, 'M1', hoverData || null, null, false
  );
}

const PLAYER = {
  name: 'Alice', participant_id: 'p-1', team_id: null, score: null
};

test('a qualified contestant carries its origin label', () => {
  const html = row(load(), { ...PLAYER, origin: 'A1' });
  assert.match(html, /<span class="lt-team-text" data-origin="A1"/);
});

test('a contestant without an origin has no data-origin attribute', () => {
  const sandbox = load();
  assert.doesNotMatch(row(sandbox, { ...PLAYER, origin: null }), /data-origin/);
  assert.doesNotMatch(row(sandbox, PLAYER), /data-origin/);
});

test('the origin label is escaped', () => {
  const html = row(load(), { ...PLAYER, origin: '"><b>' });
  assert.doesNotMatch(html, /<b>/);
  assert.match(html, /data-origin="&quot;&gt;&lt;b&gt;"/);
});

test('the origin label survives the hover-card variant', () => {
  const html = row(
    load(), { ...PLAYER, origin: 'B2' }, { seats: { 'p-1': 'A-01' } }
  );
  assert.match(html, /lt-hover-wrap lt-team-text" data-origin="B2"/);
});

test('a placeholder shows no origin label', () => {
  const html = row(load(), { name: 'TBD', origin: 'A1' });
  assert.doesNotMatch(html, /data-origin/);
});

test('the row title spells out where a contestant qualified from', () => {
  const html = row(
    load(), { ...PLAYER, origin: 'C2', origin_title: 'Group C, place 2' }
  );
  assert.match(
    html,
    /<span class="lt-team-text" data-origin="C2" title="Alice - Group C, place 2">/
  );
});

test('the origin title is escaped', () => {
  const html = row(
    load(), { ...PLAYER, origin: 'C2', origin_title: '"><b>' }
  );
  assert.doesNotMatch(html, /<b>/);
  assert.match(html, /title="Alice - &quot;&gt;&lt;b&gt;"/);
});

test('the origin title survives the hover-card variant', () => {
  const html = row(
    load(),
    { ...PLAYER, origin: 'B2', origin_title: 'Group B, place 2' },
    { seats: { 'p-1': 'A-01' } }
  );
  assert.match(
    html,
    /lt-hover-wrap lt-team-text" data-origin="B2" tabindex="0" title="Alice - Group B, place 2"/
  );
});

test('a row without an origin keeps the plain name as its title', () => {
  const sandbox = load();
  assert.match(row(sandbox, PLAYER), /<span class="lt-team-text" title="Alice">/);
  assert.match(
    row(sandbox, { ...PLAYER, origin: null, origin_title: null }),
    /<span class="lt-team-text" title="Alice">/
  );
});

test('a placeholder keeps its plain title despite an origin title', () => {
  const html = row(
    load(), { name: 'TBD', origin: 'A1', origin_title: 'Group A, place 1' }
  );
  assert.match(html, /<span class="lt-team-text" title="TBD">/);
});
