'use strict';

const test = require('node:test');
const assert = require('node:assert');

const {
  fill,
  nextSelection,
  describeSwap,
  describeMove,
  describeReplay,
  moveFields,
  stepTarget,
  tierNoticeKind,
  dropNeedsRevert,
  firstSubmit,
  routeSubmit
} = require('../../byceps/static/behavior/lan_tournament_seeding.js');

const STRINGS = {
  bye: 'Bye',
  swapped: 'Swapped: %(a)s (%(wa)s) with %(b)s (%(wb)s).',
  moved: '%(name)s moved to tier %(letter)s, now seed %(n)s.',
  moved_reset:
    '%(name)s moved to tier %(letter)s, now seed %(n)s. Layout fixes reset.',
  where_match: 'M%(n)s, position %(pos)s',
  group: 'Group %(letter)s',
  lobby: 'Lobby %(n)s',
  replayed:
    'Rebuilt from the code: %(format)s, %(n)s participants, %(fixes)s.',
  fixes_none: 'no layout fixes',
  fixes_one: '1 layout fix',
  fixes_many: '%(n)s layout fixes'
};

function slot(index, name, seedPosition) {
  return {
    index: index,
    contestant_id: name === null ? null : 'id-' + name,
    name: name,
    bye: name === null,
    seed_position: seedPosition,
    group: null
  };
}

const BRACKET = {
  format: 'SE',
  layout: {
    kind: 'bracket',
    matches: [
      { number: 1, slots: [slot(0, 'pixelwolf', 1), slot(1, null, 4)] },
      { number: 2, slots: [slot(2, 'Mettigel', 2), slot(3, 'Zahnrad', 3)] }
    ],
    groups: []
  }
};

const LOBBIES = {
  format: 'FFA',
  layout: {
    kind: 'groups',
    matches: [],
    groups: [
      {
        index: 0,
        letter: 'A',
        slots: [
          { index: 0, name: 'Bandsalat', bye: false },
          { index: 1, name: 'Zahnrad', bye: false }
        ]
      },
      {
        index: 1,
        letter: 'B',
        slots: [
          { index: 2, name: 'Quersumme', bye: false },
          { index: 3, name: 'Laserschwert', bye: false }
        ]
      }
    ]
  }
};

function tiers() {
  function person(id, name, seed) {
    return { id: id, name: name, seed: seed };
  }
  return [
    {
      index: 0,
      letter: 'A',
      contestants: [person('a', 'Ada', 1), person('b', 'Bob', 2)]
    },
    {
      index: 1,
      letter: 'B',
      contestants: [
        person('c', 'Cy', 3),
        person('d', 'Di', 4),
        person('e', 'Ed', 5)
      ]
    }
  ];
}

test('nextSelection: select, then target, then drop', () => {
  let result = nextSelection(
    { selected: null },
    { type: 'pick', key: '3' }
  );
  assert.strictEqual(result.selected, '3');
  assert.deepStrictEqual(result.effect, { type: 'selected', key: '3' });

  result = nextSelection(
    { selected: result.selected },
    { type: 'pick', key: '7' }
  );
  assert.deepStrictEqual(result.effect, {
    type: 'apply',
    from: '3',
    to: '7'
  });
  assert.strictEqual(result.selected, null);

  result = nextSelection({ selected: '3' }, { type: 'done' });
  assert.strictEqual(result.selected, null);
  assert.strictEqual(result.effect.type, 'none');
});

test('nextSelection: picking the selected one again cancels', () => {
  const result = nextSelection({ selected: '3' }, { type: 'pick', key: '3' });
  assert.strictEqual(result.selected, null);
  assert.deepStrictEqual(result.effect, { type: 'cancelled', key: '3' });
});

test('nextSelection: Escape cancels a selection and ignores none', () => {
  let result = nextSelection({ selected: '5' }, { type: 'cancel' });
  assert.strictEqual(result.selected, null);
  assert.deepStrictEqual(result.effect, { type: 'cancelled', key: '5' });

  result = nextSelection({ selected: null }, { type: 'cancel' });
  assert.strictEqual(result.selected, null);
  assert.strictEqual(result.effect.type, 'none');
});

test('nextSelection: slot 0 is a valid key', () => {
  const result = nextSelection({ selected: null }, { type: 'pick', key: 0 });
  assert.strictEqual(result.selected, 0);
  const second = nextSelection(result, { type: 'pick', key: 1 });
  assert.deepStrictEqual(second.effect, { type: 'apply', from: 0, to: 1 });
});

test('describeSwap names both players and their places', () => {
  assert.strictEqual(
    describeSwap(BRACKET, 0, 3, STRINGS),
    'Swapped: pixelwolf (M1, position 1) with Zahnrad (M2, position 3).'
  );
});

test('describeSwap names a bye by the bye string', () => {
  assert.strictEqual(
    describeSwap(BRACKET, 1, 2, STRINGS),
    'Swapped: Bye (M1, position 4) with Mettigel (M2, position 2).'
  );
});

test('describeSwap names lobbies and groups', () => {
  assert.strictEqual(
    describeSwap(LOBBIES, 1, 2, STRINGS),
    'Swapped: Zahnrad (Lobby 1) with Quersumme (Lobby 2).'
  );
  const groups = JSON.parse(JSON.stringify(LOBBIES));
  groups.format = 'RR';
  assert.strictEqual(
    describeSwap(groups, 0, 3, STRINGS),
    'Swapped: Bandsalat (Group A) with Laserschwert (Group B).'
  );
});

test('describeSwap is empty for an unknown slot', () => {
  assert.strictEqual(describeSwap(BRACKET, 0, 99, STRINGS), '');
});

test('describeMove reports tier and seed, with or without dropped fixes', () => {
  const board = { tiers: tiers() };
  assert.strictEqual(
    describeMove(board, 'd', STRINGS, false),
    'Di moved to tier B, now seed 4.'
  );
  assert.strictEqual(
    describeMove(board, 'd', STRINGS, true),
    'Di moved to tier B, now seed 4. Layout fixes reset.'
  );
  assert.strictEqual(describeMove(board, 'zz', STRINGS, false), '');
});

test('moveFields omits the reference for the end of a tier', () => {
  assert.deepStrictEqual(moveFields('a', 1, null), {
    action: 'move_tier',
    contestant_id: 'a',
    tier: '1'
  });
  assert.deepStrictEqual(moveFields('a', 0, 'b'), {
    action: 'move_tier',
    contestant_id: 'a',
    tier: '0',
    ref_id: 'b'
  });
});

test('stepTarget: up goes before the previous one, down after the next', () => {
  const board = { tiers: tiers() };
  assert.deepStrictEqual(stepTarget(board, 'd', 'up'), {
    tier: 1,
    ref_id: 'c'
  });
  assert.deepStrictEqual(stepTarget(board, 'c', 'down'), {
    tier: 1,
    ref_id: 'e'
  });
  assert.deepStrictEqual(stepTarget(board, 'd', 'down'), {
    tier: 1,
    ref_id: null
  });
});

test('stepTarget: no step past the edges of a tier', () => {
  const board = { tiers: tiers() };
  assert.strictEqual(stepTarget(board, 'c', 'up'), null);
  assert.strictEqual(stepTarget(board, 'e', 'down'), null);
  assert.strictEqual(stepTarget(board, 'zz', 'up'), null);
});

test('describeReplay counts fixes', () => {
  const board = { format_name: 'Single knockout', player_count: 12 };
  assert.strictEqual(
    describeReplay(Object.assign({ fix_count: 0 }, board), STRINGS),
    'Rebuilt from the code: Single knockout, 12 participants, no layout fixes.'
  );
  assert.strictEqual(
    describeReplay(Object.assign({ fix_count: 1 }, board), STRINGS),
    'Rebuilt from the code: Single knockout, 12 participants, 1 layout fix.'
  );
  assert.strictEqual(
    describeReplay(Object.assign({ fix_count: 3 }, board), STRINGS),
    'Rebuilt from the code: Single knockout, 12 participants, 3 layout fixes.'
  );
});

test('tierNoticeKind tells dropped fixes from a plain rebuild', () => {
  assert.strictEqual(tierNoticeKind({ fix_count: 2 }), 'notice_reset');
  assert.strictEqual(tierNoticeKind({ fix_count: 0 }), 'notice_rebuilt');
});

test('fill keeps unknown placeholders and inserts values verbatim', () => {
  assert.strictEqual(fill('%(a)s / %(b)s', { a: 'x' }), 'x / %(b)s');
  assert.strictEqual(fill('%(a)s', { a: '$& %(b)s' }), '$& %(b)s');
});

// ------------------------------------------------------------------ //
// qualification page: tie ordering and reasons

const ordering = require('../../byceps/static/behavior/lan_tournament_seeding.js');

test('moveInOrder swaps a name with its neighbour', () => {
  assert.deepStrictEqual(ordering.moveInOrder(['a', 'b', 'c'], 1, 'up'), {
    ids: ['b', 'a', 'c'],
    index: 0,
    moved: true
  });
  assert.deepStrictEqual(ordering.moveInOrder(['a', 'b', 'c'], 1, 'down'), {
    ids: ['a', 'c', 'b'],
    index: 2,
    moved: true
  });
});

test('moveInOrder leaves the order alone at both ends', () => {
  const ids = ['a', 'b'];
  assert.deepStrictEqual(ordering.moveInOrder(ids, 0, 'up'), {
    ids: ['a', 'b'],
    index: 0,
    moved: false
  });
  assert.deepStrictEqual(ordering.moveInOrder(ids, 1, 'down'), {
    ids: ['a', 'b'],
    index: 1,
    moved: false
  });
  assert.deepStrictEqual(ordering.moveInOrder(ids, 5, 'up').moved, false);
});

test('moveInOrder does not change the list it was given', () => {
  const ids = ['a', 'b', 'c'];
  ordering.moveInOrder(ids, 0, 'down');
  assert.deepStrictEqual(ids, ['a', 'b', 'c']);
});

test('describeOrderMove names the place counted from the first rank', () => {
  const strings = { order_moved: '%(name)s is now in place %(place)s.' };
  assert.strictEqual(
    ordering.describeOrderMove('Zer0day', 2, 0, strings),
    'Zer0day is now in place 2.'
  );
  assert.strictEqual(
    ordering.describeOrderMove('Zer0day', 16, 1, strings),
    'Zer0day is now in place 17.'
  );
});

test('reasonFilled wants text beyond white space', () => {
  assert.strictEqual(ordering.reasonFilled(''), false);
  assert.strictEqual(ordering.reasonFilled('  \n\t '), false);
  assert.strictEqual(ordering.reasonFilled(undefined), false);
  assert.strictEqual(ordering.reasonFilled(' Stechen '), true);
});

test('dialogLines splits a body into its non-empty lines', () => {
  assert.deepStrictEqual(ordering.dialogLines('a\n b \n\nc'), ['a', 'b', 'c']);
  assert.deepStrictEqual(ordering.dialogLines('one'), ['one']);
  assert.deepStrictEqual(ordering.dialogLines(null), []);
});

test('the ordering board is published for both surfaces', () => {
  assert.strictEqual(typeof ordering.initOrderingBoard, 'function');
  assert.strictEqual(typeof ordering.initBoard, 'function');
});

function bracketBoard(names) {
  const slots = names.map((name, index) => ({
    index: index,
    contestant_id: name === null ? null : 'id-' + name,
    name: name,
    bye: name === null
  }));
  const matches = [];
  for (let k = 0; k < slots.length / 2; k++) {
    matches.push({ number: k + 1, slots: [slots[2 * k], slots[2 * k + 1]] });
  }
  return { layout: { kind: 'bracket', matches: matches } };
}

test('separationPairs names the slots that traded places', () => {
  const before = bracketBoard(['A1', 'A2', 'B1', 'B2']);
  const after = bracketBoard(['A1', 'B1', 'A2', 'B2']);
  assert.deepStrictEqual(ordering.separationPairs(before, after), [
    ['A2', 'B1']
  ]);
});

test('separationPairs lists every swap once', () => {
  const before = bracketBoard(['a', 'b', 'c', 'd', 'e', 'f', 'g', 'h']);
  const after = bracketBoard(['a', 'd', 'c', 'b', 'e', 'h', 'g', 'f']);
  assert.deepStrictEqual(ordering.separationPairs(before, after), [
    ['b', 'd'],
    ['f', 'h']
  ]);
});

test('separationPairs is empty when nothing moved', () => {
  const board = bracketBoard(['a', 'b', 'c', 'd']);
  assert.deepStrictEqual(ordering.separationPairs(board, board), []);
});

test('describeSeparation fills the pairs into the notice', () => {
  const before = bracketBoard(['A1', 'A2', 'B1', 'B2']);
  const after = bracketBoard(['A1', 'B1', 'A2', 'B2']);
  const strings = { separated: 'Separated: %(pairs)s.' };
  assert.strictEqual(
    ordering.describeSeparation(before, after, strings),
    'Separated: A2 ↔ B1.'
  );
});

test('separationPairs ignores a three-way rotation', () => {
  const before = bracketBoard(['a', 'b', 'c', 'd']);
  const after = bracketBoard(['b', 'c', 'a', 'd']);
  assert.deepStrictEqual(ordering.separationPairs(before, after), []);
});

test('dropNeedsRevert: a dragged move is undone after a 409 or while busy', () => {
  assert.strictEqual(dropNeedsRevert({ drag: true }, 'conflict'), true);
  assert.strictEqual(dropNeedsRevert({ drag: true }, 'busy'), true);
});

test('dropNeedsRevert: a tapped move has no optimistic DOM to undo', () => {
  assert.strictEqual(dropNeedsRevert({}, 'conflict'), false);
  assert.strictEqual(dropNeedsRevert({ drag: false }, 'busy'), false);
});

test('dropNeedsRevert: other outcomes are handled elsewhere', () => {
  assert.strictEqual(dropNeedsRevert({ drag: true }, 'success'), false);
  assert.strictEqual(dropNeedsRevert(undefined, 'conflict'), false);
});

function fakeForm(attrs) {
  const map = Object.assign({}, attrs);
  return {
    map: map,
    getAttribute(name) {
      return Object.prototype.hasOwnProperty.call(map, name) ? map[name] : null;
    },
    setAttribute(name, value) {
      map[name] = String(value);
    }
  };
}

function recordingHooks() {
  const log = { posts: 0, performs: 0, confirmations: 0, onOk: null };
  return {
    log: log,
    confirm(title, onOk) {
      log.confirmations += 1;
      log.onOk = onOk;
    },
    post() {
      log.posts += 1;
    },
    perform() {
      log.performs += 1;
    }
  };
}

test('firstSubmit lets one submit through', () => {
  const form = fakeForm({});
  assert.strictEqual(firstSubmit(form), true);
  assert.strictEqual(form.map['data-lt-sent'], '1');
  assert.strictEqual(firstSubmit(form), false);
});

test('routeSubmit: a plain form posts natively once', () => {
  const form = fakeForm({});
  const hooks = recordingHooks();
  assert.strictEqual(routeSubmit(form, false, hooks), true);
  assert.strictEqual(routeSubmit(form, false, hooks), false);
  assert.strictEqual(hooks.log.posts, 0);
});

test('routeSubmit: a confirmed native form posts after the confirmation only', () => {
  const form = fakeForm({ 'data-confirm-title': 'Regenerate?' });
  const hooks = recordingHooks();
  assert.strictEqual(routeSubmit(form, false, hooks), false);
  assert.strictEqual(hooks.log.confirmations, 1);
  assert.strictEqual(hooks.log.posts, 0);
  assert.strictEqual(form.getAttribute('data-lt-sent'), null);
  hooks.log.onOk();
  assert.strictEqual(hooks.log.posts, 1);
  assert.strictEqual(form.map['data-lt-sent'], '1');
});

test('routeSubmit: a cancelled confirmation leaves the form retryable', () => {
  const form = fakeForm({ 'data-confirm-title': 'Regenerate?' });
  const hooks = recordingHooks();
  routeSubmit(form, false, hooks);
  assert.strictEqual(form.getAttribute('data-lt-sent'), null);
  routeSubmit(form, false, hooks);
  hooks.log.onOk();
  assert.strictEqual(hooks.log.posts, 1);
});

test('routeSubmit: a double confirmation posts once', () => {
  const form = fakeForm({ 'data-confirm-title': 'Regenerate?' });
  const hooks = recordingHooks();
  routeSubmit(form, false, hooks);
  hooks.log.onOk();
  hooks.log.onOk();
  assert.strictEqual(hooks.log.posts, 1);
});

test('routeSubmit: an action form never consumes the guard', () => {
  const form = fakeForm({});
  const hooks = recordingHooks();
  assert.strictEqual(routeSubmit(form, true, hooks), false);
  assert.strictEqual(hooks.log.performs, 1);
  assert.strictEqual(form.getAttribute('data-lt-sent'), null);
});
