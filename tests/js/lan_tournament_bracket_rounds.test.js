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

const STRINGS = {
  round: 'Runde',
  final: 'Finale',
  semifinal: 'Halbfinale',
  quarterfinal: 'Viertelfinale'
};

function titles(sandbox, count, isMainBracket, strings) {
  const out = [];
  for (let i = 0; i < count; i++) {
    out.push(sandbox.roundTitle(i, count, isMainBracket, strings));
  }
  return out;
}

test('roundTitle: final, semifinal, quarterfinal', () => {
  const sandbox = load();
  assert.deepStrictEqual(titles(sandbox, 3, true, STRINGS), [
    'Viertelfinale', 'Halbfinale', 'Finale'
  ]);
  assert.deepStrictEqual(titles(sandbox, 1, true, STRINGS), ['Finale']);
  assert.deepStrictEqual(titles(sandbox, 2, true, STRINGS), [
    'Halbfinale', 'Finale'
  ]);
  assert.deepStrictEqual(titles(sandbox, 3, true, null), [
    'Quarterfinal', 'Semifinal', 'Final'
  ]);
});

test('roundTitle: early rounds keep Runde n', () => {
  const sandbox = load();
  assert.deepStrictEqual(titles(sandbox, 5, true, STRINGS), [
    'Runde 1', 'Runde 2', 'Viertelfinale', 'Halbfinale', 'Finale'
  ]);
});

test('roundTitle: losers bracket unchanged', () => {
  const sandbox = load();
  assert.deepStrictEqual(titles(sandbox, 4, false, STRINGS), [
    'Runde 1', 'Runde 2', 'Runde 3', 'Runde 4'
  ]);
});
