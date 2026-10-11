'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const SOURCE = fs.readFileSync(path.join(
  __dirname, '../../byceps/static/behavior/lan_tournament_bracket.js'
), 'utf8');

function load() {
  const sandbox = {
    document: {
      addEventListener() {}, getElementById() { return null; },
      createElement(tagName) {
        return {
          tagName, style: {}, attributes: {},
          setAttribute(key, value) { this.attributes[key] = value; },
          getAttribute(key) { return this.attributes[key]; }
        };
      }
    },
    window: { addEventListener() {} }
  };
  vm.createContext(sandbox);
  vm.runInContext(SOURCE, sandbox);
  return sandbox;
}

const PLAYER = { name: 'Alice', participant_id: 'a', score: null };

function render(sandbox, raw, ref, options) {
  const match = sandbox.parseBracketData({
    tournament: {},
    matches: [{
      id: 'm', round: 0, match_order: 0, confirmed: false,
      contestants: [PLAYER, { ...PLAYER, name: 'Bob', participant_id: 'b' }],
      ...raw
    }]
  }).matchMap[ref];
  match.geom = { boxLeft: 0, boxTop: 0 };
  return sandbox.createMatchEl(match, {
    matchWidth: 240, padding: 8, rowGap: 4, labelHeight: 24,
    teamHeight: 28, fontScale: 1
  }, options, {});
}

function refText(el) {
  return /<span class="lt-match-ref-text">([^<]*)<\/span>/.exec(el.innerHTML)[1];
}

for (const [bracket, ref, long, stage] of [
  ['GF', 'GF M1', 'Grand Final – Game 1', 'Grand Final'],
  ['P3', 'P3 M1', '3rd Place – Game 1', '3rd Place']
]) {
  test(`${ref} with a stage line keeps only the game number in the label`, () => {
    const el = render(load(), { bracket }, ref, { stageText: stage });
    assert.equal(refText(el), 'Game 1');
    assert.ok(el.innerHTML.includes(
      `<span class="lt-match-stage" title="${stage}">${stage}</span>`
    ));
    assert.ok(el.title.startsWith(long));
    assert.ok(el.attributes['aria-label'].startsWith(long));
  });

  test(`${ref} without a stage line keeps the long label`, () => {
    const el = render(load(), { bracket }, ref, {});
    assert.equal(refText(el), long);
  });
}

test('a bracket-reset game keeps its own number', () => {
  const el = render(
    load(), { bracket: 'GF', match_order: 1 }, 'GF M2',
    { stageText: 'Bracket Reset' }
  );
  assert.equal(refText(el), 'Game 2');
});

test('winner and loser bracket labels are unchanged', () => {
  const el = render(
    load(), { bracket: 'WB', round: 2 }, 'WB R3 M1', { stageText: 'Round 3' }
  );
  assert.equal(refText(el), 'WB R3 – Game 1');
});
