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
function raw(overrides = {}) {
  return {
    id: 'm', round: 0, match_order: 0, bracket: null, confirmed: false,
    contestants: [PLAYER, { ...PLAYER, name: 'Bob', participant_id: 'b' }],
    ...overrides
  };
}
function readiness(status = 'both_ready', label = 'Beide bereit', sides = ['a', 'b']) {
  return { supported: true, status, label, ready_sides: sides };
}
function parse(sandbox, rows) {
  return sandbox.parseBracketData({ tournament: {}, matches: rows }).matchMap['R1 M1'];
}
function render(sandbox, match) {
  match.geom = { boxLeft: 0, boxTop: 0 };
  return sandbox.createMatchEl(match, {
    matchWidth: 240, padding: 8, rowGap: 4, labelHeight: 24,
    teamHeight: 28, fontScale: 1
  }, {}, {});
}

for (const [status, label, sides] of [
  ['not_yet_occupied', 'Wartet auf Gegner', []],
  ['open', 'Nicht bereit', []],
  ['partially_ready', 'Seite A bereit', ['a']],
  ['partially_ready', 'Seite B bereit', ['b']],
  ['both_ready', 'Beide bereit', ['a', 'b']]
]) {
  test(`canonical ${status}/${sides} renders without changing assignment status`, () => {
    const sandbox = load();
    const facts = readiness(status, label, sides);
    const match = parse(sandbox, [raw({ readiness: facts })]);
    assert.equal(match.readiness, facts);
    assert.equal(match.isReadyToPlay, true);
    assert.equal(match.isComplete, false);
    assert.equal(match.winnerIndex, null);
    const el = render(sandbox, match);
    assert.equal(el.attributes['data-match-status'], 'open');
    assert.equal(el.attributes['data-readiness-status'], status);
    assert.equal(el.attributes['data-ready-sides'], sides.join(','));
    assert.ok(el.innerHTML.includes(label));
    assert.ok(el.attributes['aria-label'].includes(label));
    assert.doesNotMatch(el.innerHTML, /class="lt-match-status/);
    if (status === 'open') {
      assert.match(el.innerHTML, /class="lt-match-readiness"[^>]*>Nicht bereit<\/span>/);
      assert.deepEqual(facts.ready_sides, []);
      assert.doesNotMatch(el.innerHTML, /Both ready|Partially ready|Beide bereit|Seite [AB] bereit/);
    }
  });
}

test('old payload, clocks and email markers never manufacture readiness', () => {
  const sandbox = load();
  const match = parse(sandbox, [raw({
    ready_at_a: '2026-10-05', ready_at_b: '2026-10-05',
    occupied_since: '2026-10-04', both_ready_notified_at: '2026-10-05'
  })]);
  assert.equal(match.readiness, null);
  const el = render(sandbox, match);
  assert.equal(el.attributes['data-match-status'], 'open');
  assert.equal(el.attributes['data-readiness-status'], undefined);
  assert.doesNotMatch(el.innerHTML, /lt-match-readiness/);
});

test('unsupported FFA and missing translated label render no readiness badge', () => {
  for (const facts of [{ ...readiness(), supported: false }, { ...readiness(), label: null }]) {
    const sandbox = load();
    const el = render(sandbox, parse(sandbox, [raw({ readiness: facts })]));
    assert.equal(el.attributes['data-readiness-status'], undefined);
    assert.doesNotMatch(el.innerHTML, /lt-match-readiness/);
  }
});

test('server labels and status attributes are escaped, not mapped to English', () => {
  const sandbox = load();
  const el = render(sandbox, parse(sandbox, [raw({
    readiness: readiness('custom"state', '<Bereit & "jetzt">', ['b'])
  })]));
  assert.match(el.innerHTML, /&lt;Bereit &amp; &quot;jetzt&quot;&gt;/);
  assert.match(el.innerHTML, /custom&quot;state/);
  assert.doesNotMatch(el.innerHTML, /Both ready|Partially ready|<Bereit/);
});

test('played scores and winner retain precedence over a canonical outcome', () => {
  const sandbox = load();
  const match = parse(sandbox, [raw({
    confirmed: true,
    contestants: [{ ...PLAYER, score: 3 }, { ...PLAYER, participant_id: 'b', score: 1 }],
    readiness: readiness('confirmed', 'Bestätigt', [])
  })]);
  assert.equal(match.isComplete, true);
  assert.equal(match.winnerIndex, 1);
  assert.equal(render(sandbox, match).attributes['data-match-status'], 'done');
});

test('bye/defwin remains auto-advanced even with independent readiness facts', () => {
  const sandbox = load();
  const match = parse(sandbox, [raw({
    contestants: [PLAYER], readiness: readiness('defwin', 'Freilos', [])
  })]);
  assert.equal(match.isAutoAdvanced, true);
  assert.equal(match.winnerIndex, 1);
  const el = render(sandbox, match);
  assert.equal(el.attributes['data-match-status'], 'auto');
  assert.equal(el.attributes['data-readiness-status'], undefined);
  assert.match(el.innerHTML, /class="lt-match-status lt-match-status--auto"/);
  assert.doesNotMatch(el.innerHTML, /lt-match-readiness|Freilos/);
});

test('pending feeder stays pending and is not completed by both-ready facts', () => {
  const sandbox = load();
  const rows = [
    raw({ contestants: [PLAYER], readiness: readiness() }),
    raw({ id: 'feeder', round: 1, next_match_id: 'm' }),
    raw({ id: 'other-feeder', round: 1, match_order: 1, next_match_id: 'm' })
  ];
  const match = parse(sandbox, rows);
  assert.equal(match.isWaitingForPlayers, true);
  assert.equal(match.isComplete, false);
  assert.equal(match.isAutoAdvanced, false);
  assert.equal(match.winnerIndex, null);
  const el = render(sandbox, match);
  assert.equal(el.attributes['data-match-status'], 'pending');
  assert.equal(el.attributes['data-readiness-status'], 'both_ready');
});

test('finished match shows no readiness badge', () => {
  for (const [status, label, rows] of [
    ['confirmed', 'Bestätigt', { confirmed: true }],
    ['defwin', 'Freilos', { confirmed: true, contestants: [PLAYER] }],
    ['completed', 'Abgeschlossen', { confirmed: true }],
    ['cancelled', 'Abgebrochen', {}]
  ]) {
    const sandbox = load();
    const match = parse(sandbox, [raw({ ...rows, readiness: readiness(status, label, []) })]);
    const el = render(sandbox, match);
    assert.equal(el.attributes['data-readiness-status'], undefined, status);
    assert.equal(el.attributes['data-ready-sides'], undefined, status);
    assert.doesNotMatch(el.innerHTML, /lt-match-readiness/, status);
    assert.ok(!el.innerHTML.includes(label), status);
    assert.ok(!el.attributes['aria-label'].includes(label), status);
    assert.ok(!el.title.includes(label), status);
    assert.equal(el.innerHTML.match(/class="lt-match-status /g).length, 1, status);
  }
  const sandbox = load();
  sandbox._ltStrings = { statusDone: 'Abgeschlossen' };
  const el = render(sandbox, parse(sandbox, [raw({
    confirmed: true, readiness: readiness('confirmed', 'Bestätigt', [])
  })]));
  assert.equal(el.attributes['data-match-status'], 'done');
  assert.match(el.innerHTML, /<span class="lt-match-status lt-match-status--done"[^>]*>Abgeschlossen<\/span>/);
});

test('unfinished match shows the readiness badge instead of the Open tag', () => {
  const sandbox = load();
  sandbox._ltStrings = { statusOpen: 'Offen' };
  const el = render(sandbox, parse(sandbox, [raw({
    readiness: readiness('open', 'Nicht bereit', [])
  })]));
  assert.equal(el.attributes['data-match-status'], 'open');
  assert.equal(el.attributes['data-readiness-status'], 'open');
  assert.match(el.innerHTML, /<span class="lt-match-readiness"[^>]*>Nicht bereit<\/span>/);
  assert.match(el.innerHTML, /lt-match-state-dot lt-match-state-dot--open/);
  assert.doesNotMatch(el.innerHTML, /class="lt-match-status/);
  assert.doesNotMatch(el.innerHTML, />Offen</);
});

test('unassigned readiness match shows Wartet auf Gegner instead of Ausstehend', () => {
  const sandbox = load();
  sandbox._ltStrings = { statusPending: 'Ausstehend' };
  const el = render(sandbox, parse(sandbox, [raw({
    contestants: [],
    readiness: readiness('not_yet_occupied', 'Wartet auf Gegner', [])
  })]));
  assert.equal(el.attributes['data-match-status'], 'pending');
  assert.equal(el.attributes['data-readiness-status'], 'not_yet_occupied');
  assert.match(el.innerHTML, /<span class="lt-match-readiness"[^>]*>Wartet auf Gegner<\/span>/);
  assert.match(el.innerHTML, /lt-match-state-dot lt-match-state-dot--pending/);
  assert.doesNotMatch(el.innerHTML, /class="lt-match-status/);
  assert.doesNotMatch(el.innerHTML, />Ausstehend</);
});

test('unsupported format keeps the status tag and its Ausstehend word', () => {
  const sandbox = load();
  sandbox._ltStrings = { statusPending: 'Ausstehend' };
  const el = render(sandbox, parse(sandbox, [raw({
    contestants: [], readiness: { ...readiness('not_yet_occupied', 'Wartet auf Gegner', []), supported: false }
  })]));
  assert.equal(el.attributes['data-readiness-status'], undefined);
  assert.match(el.innerHTML, /<span class="lt-match-status lt-match-status--pending"[^>]*>Ausstehend<\/span>/);
  assert.doesNotMatch(el.innerHTML, /lt-match-readiness|Wartet auf Gegner/);
});

test('inherited object keys are not treated as outcomes', () => {
  for (const status of ['constructor', 'toString', '__proto__', 'hasOwnProperty']) {
    const sandbox = load();
    const el = render(sandbox, parse(sandbox, [raw({
      readiness: readiness(status, 'Nicht bereit', [])
    })]));
    assert.equal(el.attributes['data-readiness-status'], status);
    assert.match(el.innerHTML, /lt-match-readiness/, status);
    assert.doesNotMatch(el.innerHTML, /class="lt-match-status/, status);
  }
});
