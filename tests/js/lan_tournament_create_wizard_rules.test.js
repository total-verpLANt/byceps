'use strict';

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const rules = require('../../byceps/static/behavior/lan_tournament_create_wizard_rules.js');

const CASES = JSON.parse(
  fs.readFileSync(
    path.join(
      __dirname,
      '../unit/services/lan_tournament/data/create_wizard_rule_cases.json'
    ),
    'utf8'
  )
);

const VALID_COMBINATIONS = {
  FREE_FOR_ALL: ['DOUBLE_ELIMINATION', 'SINGLE_ELIMINATION'],
  HIGHSCORE: ['NONE'],
  ONE_V_ONE: ['DOUBLE_ELIMINATION', 'ROUND_ROBIN', 'SINGLE_ELIMINATION'],
};

const CTX = {
  validCombinations: VALID_COMBINATIONS,
  limits: {countMax: 1024, pointTableMax: 64},
  capacity: 240,
  requireStructure: true,
};

const CLIENT_ONLY_FIELDS = new Set(rules.STEP_FIELDS[0].concat(['req_map']));

function basics(values) {
  return Object.assign({name: 'Cup', category: 'MAIN'}, values);
}

function errorsOf(values, ctx) {
  return rules.validate(basics(values), Object.assign({}, CTX, ctx)).errors;
}

test('parity cases', async (t) => {
  for (const c of CASES) {
    await t.test(c.name, () => {
      const result = rules.validate(
        c.values,
        Object.assign({}, CTX, {requireStructure: c.require_structure})
      );
      const actual = {};
      for (const [field, msg] of Object.entries(result.errors)) {
        if (!CLIENT_ONLY_FIELDS.has(field)) {
          actual[field] = msg.msgid;
        }
      }
      assert.deepStrictEqual(actual, c.expected);
    });
  }
});

test('parity params match the Python params', () => {
  const solo = errorsOf({
    contestant_type: 'SOLO',
    game_format: 'ONE_V_ONE',
    elimination_mode: 'SINGLE_ELIMINATION',
    min_players: '16',
    max_players: '8',
  });
  assert.deepStrictEqual(solo.max_players.params, {
    other: 'Min. players',
    n: 16,
  });

  const ffa = errorsOf({
    contestant_type: 'TEAM',
    game_format: 'FREE_FOR_ALL',
    elimination_mode: 'DOUBLE_ELIMINATION',
    max_teams: '3',
    point_table: ['10'],
    group_size_min: '4',
    group_size_max: '6',
    advancement_count: '4',
  });
  assert.deepStrictEqual(ffa.group_size_min.params, {n: 3, min: 4});
  assert.deepStrictEqual(ffa.advancement_count.params, {n: 4});
});

test('client form rules', async (t) => {
  await t.test('name is required and stripped', () => {
    assert.strictEqual(errorsOf({name: '   '}).name.msgid, 'Please enter a name.');
    assert.strictEqual(errorsOf({name: ' Cup '}).name, undefined);
  });

  await t.test('length rules count code points', () => {
    const long = errorsOf({name: 'x'.repeat(81)}).name;
    assert.strictEqual(long.msgid, 'At most %(max)d characters – currently %(length)d.');
    assert.deepStrictEqual(long.params, {max: 80, length: 81});
    assert.strictEqual(errorsOf({name: '😀'.repeat(80)}).name, undefined);
    assert.deepStrictEqual(errorsOf({game: 'g'.repeat(81)}).game.params, {max: 80, length: 81});
    assert.strictEqual(errorsOf({description: 'd'.repeat(10001)}).description.params.max, 10000);
    assert.strictEqual(errorsOf({ruleset: 'r'.repeat(10000)}).ruleset, undefined);
  });

  await t.test('image url scheme', () => {
    for (const bad of ['ftp://bilder.local/x', 'javascript:alert(1)', 'https://', 'example.org/x.png']) {
      assert.ok(errorsOf({image_url: bad, urlMode: true}).image_url, bad);
    }
    assert.strictEqual(errorsOf({image_url: 'https://example.org/a.png', urlMode: true}).image_url, undefined);
    assert.strictEqual(errorsOf({image_url: 'ftp://x', urlMode: false}).image_url, undefined);
  });

  await t.test('whole numbers from 1', () => {
    for (const bad of ['0', '-3', '1e2', '2.5', 'abc', '١٢']) {
      const e = errorsOf({max_players: bad}).max_players;
      assert.strictEqual(e && e.msgid, 'Whole numbers from 1 only.', bad);
    }
    assert.strictEqual(errorsOf({max_players: ' 12 '}).max_players, undefined);
    assert.strictEqual(errorsOf({max_players: ''}).max_players, undefined);
    assert.strictEqual(errorsOf({group_size_max: '1'}).group_size_max.msgid, 'At least 2.');
  });

  await t.test('req_map fires only without a decision', () => {
    const ctx = {fromRequest: true};
    const values = {contestant_type: 'SOLO'};
    assert.strictEqual(
      errorsOf(values, ctx).req_map.msgid,
      'Please decide whether to apply the participant values from the request.'
    );
    assert.strictEqual(errorsOf(Object.assign({reqMap: 'applied'}, values), ctx).req_map, undefined);
    assert.strictEqual(errorsOf(Object.assign({reqMap: 'declined'}, values), ctx).req_map, undefined);
    assert.strictEqual(errorsOf(values, {fromRequest: false}).req_map, undefined);
    assert.strictEqual(errorsOf({}, ctx).req_map, undefined);
  });

  await t.test('form errors win over domain errors per field', () => {
    const e = errorsOf({
      contestant_type: 'SOLO',
      game_format: 'FREE_FOR_ALL',
      elimination_mode: 'SINGLE_ELIMINATION',
      point_table: ['1'],
      group_size_max: '1',
    });
    assert.strictEqual(e.group_size_max.msgid, 'At least 2.');
  });

  await t.test('warnings', () => {
    const solo = rules.validate(basics({contestant_type: 'SOLO', max_players: '300'}), CTX);
    assert.deepStrictEqual(solo.errors.max_players, undefined);
    assert.deepStrictEqual(solo.warnings.max_players.params, {seats: 240});

    const team = rules.validate(basics({contestant_type: 'TEAM', max_teams: '100', max_players_in_team: '3'}), CTX);
    assert.deepStrictEqual(team.warnings.max_teams.params, {t: 100, p: 3, n: 300, seats: 240});

    const noCapacity = rules.validate(basics({contestant_type: 'SOLO', max_players: '300'}), {});
    assert.deepStrictEqual(noCapacity.warnings, {});

    const short = rules.validate(basics({
      contestant_type: 'SOLO',
      game_format: 'FREE_FOR_ALL',
      elimination_mode: 'SINGLE_ELIMINATION',
      point_table: ['10', '8'],
      group_size_max: '6',
    }), CTX);
    assert.deepStrictEqual(short.warnings.point_table.params, {from: 3, to: 6});
  });
});

test('count ceiling', () => {
  const e = errorsOf({max_teams: '1025'}, {limits: {countMax: 1024}}).max_teams;
  assert.strictEqual(e.msgid, 'At most %(max)s.');
  assert.deepStrictEqual(e.params, {max: 1024});
  assert.strictEqual(errorsOf({max_teams: '1024'}).max_teams, undefined);
  assert.strictEqual(errorsOf({max_teams: '5'}, {limits: {countMax: 4}}).max_teams.params.max, 4);
  assert.strictEqual(errorsOf({max_teams: '9'.repeat(400)}).max_teams.msgid, 'At most %(max)s.');
});

test('dependent change notices', async (t) => {
  await t.test('FFA to 1v1 warns about the dropped FFA settings and undoes', () => {
    const prev = {
      game_format: 'FREE_FOR_ALL',
      elimination_mode: 'SINGLE_ELIMINATION',
      point_table: ['10', '5'],
      group_size_max: '4',
    };
    const {values, notices} = rules.dependentChange(prev, 'game_format', 'ONE_V_ONE', CTX);
    assert.strictEqual(values.game_format, 'ONE_V_ONE');
    assert.strictEqual(values.elimination_mode, 'SINGLE_ELIMINATION');
    assert.strictEqual(notices.length, 1);
    assert.deepStrictEqual(notices[0].params, {format: '1v1'});
    assert.deepStrictEqual(Object.assign({}, values, notices[0].undo), prev);
    assert.strictEqual(prev.game_format, 'FREE_FOR_ALL');
  });

  await t.test('round robin to FFA resets the mode', () => {
    const prev = {game_format: 'ONE_V_ONE', elimination_mode: 'ROUND_ROBIN'};
    const {values, notices} = rules.dependentChange(prev, 'game_format', 'FREE_FOR_ALL', CTX);
    assert.strictEqual(values.elimination_mode, null);
    assert.strictEqual(notices.length, 1);
    assert.deepStrictEqual(notices[0].params, {mode: 'Everyone plays everyone', format: 'Free-for-All'});
    assert.deepStrictEqual(Object.assign({}, values, notices[0].undo), prev);
  });

  await t.test('a mode kept by the new format raises no notice', () => {
    const {notices} = rules.dependentChange(
      {game_format: 'ONE_V_ONE', elimination_mode: 'DOUBLE_ELIMINATION'}, 'game_format', 'FREE_FOR_ALL', CTX
    );
    assert.deepStrictEqual(notices, []);
  });

  await t.test('Highscore sets mode NONE; leaving it silently clears NONE', () => {
    const toHigh = rules.dependentChange({game_format: 'ONE_V_ONE', elimination_mode: 'ROUND_ROBIN'}, 'game_format', 'HIGHSCORE', CTX);
    assert.strictEqual(toHigh.values.elimination_mode, 'NONE');
    assert.deepStrictEqual(toHigh.notices, []);

    const fromHigh = rules.dependentChange({game_format: 'HIGHSCORE', elimination_mode: 'NONE', score_ordering: 'HIGHER_IS_BETTER'}, 'game_format', 'ONE_V_ONE', CTX);
    assert.strictEqual(fromHigh.values.elimination_mode, null);
    assert.strictEqual(fromHigh.notices.length, 1);
    assert.strictEqual(fromHigh.notices[0].msgid, 'Score ordering no longer applies. It is only saved for Highscore.');
  });

  await t.test('Solo to Teams lists the dropped Solo values and undoes', () => {
    const prev = {contestant_type: 'SOLO', min_players: '4', max_players: '32', reqMap: 'declined'};
    const {values, notices} = rules.dependentChange(prev, 'contestant_type', 'TEAM', CTX);
    assert.strictEqual(values.contestant_type, 'TEAM');
    assert.strictEqual(values.reqMap, null);
    assert.strictEqual(notices.length, 1);
    assert.strictEqual(notices[0].params.type, 'Teams');
    assert.strictEqual(notices[0].params.other, 'Solo');
    assert.deepStrictEqual(notices[0].params.fields, [
      {label: 'Min. players', value: '4'},
      {label: 'Max. players', value: '32'},
    ]);
    assert.deepStrictEqual(Object.assign({}, values, notices[0].undo), prev);
  });

  await t.test('no notice for an empty first choice or an unchanged value', () => {
    assert.deepStrictEqual(rules.dependentChange({}, 'contestant_type', 'SOLO', CTX).notices, []);
    const same = rules.dependentChange({game_format: 'ONE_V_ONE'}, 'game_format', 'ONE_V_ONE', CTX);
    assert.deepStrictEqual(same.notices, []);
  });

  await t.test('at most two notices', () => {
    const ctx = {validCombinations: {ONE_V_ONE: ['SINGLE_ELIMINATION']}};
    const prev = {
      game_format: 'FREE_FOR_ALL',
      elimination_mode: 'DOUBLE_ELIMINATION',
      point_table: ['1'],
      score_ordering: 'HIGHER_IS_BETTER',
    };
    const {notices} = rules.dependentChange(prev, 'game_format', 'ONE_V_ONE', ctx);
    assert.strictEqual(notices.length, 2);
  });

  await t.test('other fields pass through', () => {
    const {values, notices} = rules.dependentChange({name: 'a'}, 'name', 'b', CTX);
    assert.deepStrictEqual(values, {name: 'b'});
    assert.deepStrictEqual(notices, []);
  });
});

test('step status and skip', async (t) => {
  const state = (over) => Object.assign({step: 1, visited: {}, values: {}}, over);

  const SE_1V1 = {game_format: 'ONE_V_ONE', elimination_mode: 'SINGLE_ELIMINATION'};
  const RR_1V1 = {game_format: 'ONE_V_ONE', elimination_mode: 'ROUND_ROBIN'};

  await t.test('1v1 skips the scoring step in both directions', () => {
    assert.strictEqual(rules.skipScoring(SE_1V1), true);
    assert.strictEqual(rules.skipScoring(RR_1V1), true);
    assert.strictEqual(rules.nextStep(1, SE_1V1), 2);
    assert.strictEqual(rules.prevStep(3, SE_1V1), 2);
  });

  await t.test('1v1 single knockout skips scoring and playoffs', () => {
    assert.strictEqual(rules.skipPlayoffs(SE_1V1), true);
    assert.strictEqual(rules.nextStep(2, SE_1V1), 5);
    assert.strictEqual(rules.prevStep(5, SE_1V1), 2);
  });

  await t.test('1v1 round robin skips scoring but keeps the playoffs', () => {
    assert.strictEqual(rules.skipPlayoffs(RR_1V1), false);
    assert.strictEqual(rules.nextStep(2, RR_1V1), 4);
    assert.strictEqual(rules.nextStep(4, RR_1V1), 5);
    assert.strictEqual(rules.prevStep(5, RR_1V1), 4);
    assert.strictEqual(rules.prevStep(4, RR_1V1), 2);
  });

  await t.test('highscore visits scoring and playoffs', () => {
    const v = {game_format: 'HIGHSCORE', elimination_mode: 'NONE'};
    assert.strictEqual(rules.skipScoring(v), false);
    assert.strictEqual(rules.skipPlayoffs(v), false);
    assert.strictEqual(rules.nextStep(2, v), 3);
    assert.strictEqual(rules.nextStep(3, v), 4);
    assert.strictEqual(rules.prevStep(5, v), 4);
    assert.strictEqual(rules.prevStep(4, v), 3);
  });

  await t.test('free-for-all and plain knockout skip only the playoffs', () => {
    for (const v of [
      {game_format: 'FREE_FOR_ALL', elimination_mode: 'DOUBLE_ELIMINATION'},
      {game_format: 'FREE_FOR_ALL'},
      {},
    ]) {
      assert.strictEqual(rules.skipScoring(v), false);
      assert.strictEqual(rules.skipPlayoffs(v), true);
      assert.strictEqual(rules.nextStep(2, v), 3);
      assert.strictEqual(rules.nextStep(3, v), 5);
      assert.strictEqual(rules.prevStep(5, v), 3);
      assert.strictEqual(rules.nextStep(5, v), 5);
      assert.strictEqual(rules.prevStep(0, v), 0);
    }
  });

  await t.test('cur, done, err, skip, todo', () => {
    const s = state({visited: {0: true, 1: true, 2: true}, values: SE_1V1});
    assert.strictEqual(rules.stepStatus(1, s, {}), 'cur');
    assert.strictEqual(rules.stepStatus(0, s, {}), 'done');
    assert.strictEqual(rules.stepStatus(0, s, {name: {msgid: 'x'}}), 'err');
    assert.strictEqual(rules.stepStatus(2, s, {min_players: {msgid: 'x'}}), 'err');
    assert.strictEqual(rules.stepStatus(3, s, {}), 'skip');
    assert.strictEqual(rules.stepStatus(4, s, {}), 'skip');
    assert.strictEqual(rules.stepStatus(5, s, {}), 'todo');
  });

  await t.test('skip needs a visited step 3', () => {
    const s = state({visited: {}, values: SE_1V1});
    assert.strictEqual(rules.stepStatus(3, s, {}), 'todo');
    assert.strictEqual(rules.stepStatus(4, s, {}), 'todo');
  });

  await t.test('the playoffs step of a live format is never skipped', () => {
    const s = state({visited: {0: true, 1: true, 2: true, 3: true}, values: RR_1V1});
    assert.strictEqual(rules.stepStatus(4, s, {}), 'todo');
    const hs = state({
      visited: {0: true, 1: true, 2: true, 3: true, 4: true},
      values: {game_format: 'HIGHSCORE'},
    });
    assert.strictEqual(rules.stepStatus(4, hs, {}), 'done');
    assert.strictEqual(
      rules.stepStatus(4, hs, {playoff_release_mode: {msgid: 'x'}}), 'err');
  });

  await t.test('free-for-all skips the playoffs once the scoring was seen', () => {
    const ffa = {game_format: 'FREE_FOR_ALL'};
    assert.strictEqual(
      rules.stepStatus(4, state({visited: {2: true}, values: ffa}), {}), 'todo');
    assert.strictEqual(
      rules.stepStatus(4, state({visited: {2: true, 3: true}, values: ffa}), {}),
      'skip');
  });

  await t.test('highscore free-for-all errors belong to the playoffs step', () => {
    const hs = state({
      visited: {0: true, 1: true, 2: true, 3: true, 4: true},
      values: {game_format: 'HIGHSCORE'},
    });
    const errors = {point_table: {msgid: 'x'}};
    assert.strictEqual(rules.stepStatus(3, hs, errors), 'done');
    assert.strictEqual(rules.stepStatus(4, hs, errors), 'err');
    const ffa = state({
      visited: {0: true, 1: true, 2: true, 3: true},
      values: {game_format: 'FREE_FOR_ALL'},
    });
    assert.strictEqual(rules.stepStatus(3, ffa, errors), 'err');
  });

  await t.test('errors on an unvisited step stay todo', () => {
    assert.strictEqual(rules.stepStatus(0, state(), {name: {msgid: 'x'}}), 'todo');
  });
});

test('applicable scopes', () => {
  const s = rules.applicableScopes({
    contestant_type: 'TEAM',
    game_format: 'FREE_FOR_ALL',
    elimination_mode: 'DOUBLE_ELIMINATION',
  });
  assert.deepStrictEqual(s, {
    solo: false, team: true, highscore: false, ffa: true, ffaFormat: true,
    ffaPlayoff: false, ffaDe: true, oneVOne: false, playoffNone: true,
    playoffEligible: false, playoffKindRr: false, playoffKindHs: false,
    playoffRr: false, playoffHs: false, playoffOn: false,
  });
  assert.strictEqual(rules.applicableScopes({game_format: 'FREE_FOR_ALL', elimination_mode: 'SINGLE_ELIMINATION'}).ffaDe, false);
  assert.strictEqual(rules.applicableScopes({}).solo, false);
});

test('format placeholders', () => {
  assert.strictEqual(rules.format('Step %(n)s of %(total)s', {n: 2, total: 5}), 'Step 2 of 5');
  assert.strictEqual(rules.format('At most %(max)d characters – currently %(length)d.', {max: 80, length: 81}), 'At most 80 characters – currently 81.');
  assert.strictEqual(rules.format('%(n)s and %(n)s', {n: 4}), '4 and 4');
  assert.strictEqual(rules.format('Keep %(missing)s', {}), 'Keep %(missing)s');
  assert.strictEqual(rules.format('100%% sure %(n)s', {n: 1}), '100% sure 1');
  assert.strictEqual(rules.format('%(n)s', {n: 0}), '0');
});

test('request values', async (t) => {
  const team = {derivedContestantType: 'TEAM', teamSize: 3, participantLimit: 16};
  const solo = {derivedContestantType: 'SOLO', teamSize: 1, participantLimit: 32};

  await t.test('initialReqMap', () => {
    assert.strictEqual(rules.initialReqMap({contestant_type: 'TEAM'}, team), 'applied');
    assert.strictEqual(rules.initialReqMap({contestant_type: 'SOLO'}, team), null);
    assert.strictEqual(rules.initialReqMap({contestant_type: 'SOLO'}, solo), 'applied');
    assert.strictEqual(rules.initialReqMap({contestant_type: 'TEAM'}, null), null);
    assert.strictEqual(rules.initialReqMap({}, team), null);
  });

  await t.test('a Solo/Teams change resets to undecided', () => {
    const prev = {contestant_type: 'TEAM', reqMap: 'applied'};
    const {values} = rules.dependentChange(prev, 'contestant_type', 'SOLO', CTX);
    assert.strictEqual(values.reqMap, null);
    assert.strictEqual(rules.dependentChange(prev, 'game_format', 'ONE_V_ONE', CTX).values.reqMap, 'applied');
  });

  await t.test('applyRequestValues maps TEAM and SOLO', () => {
    const t = rules.applyRequestValues({contestant_type: 'TEAM', max_players: '9'}, team);
    assert.deepStrictEqual(
      [t.min_players_in_team, t.max_players_in_team, t.max_teams, t.max_players, t.reqMap],
      ['3', '3', '16', '9', 'applied']
    );
    const s = rules.applyRequestValues({contestant_type: 'SOLO', max_teams: '7'}, solo);
    assert.deepStrictEqual([s.max_players, s.max_teams, s.reqMap], ['32', '7', 'applied']);
  });

  await t.test('applyRequestValues does not mutate its input', () => {
    const input = {contestant_type: 'SOLO'};
    rules.applyRequestValues(input, solo);
    assert.deepStrictEqual(input, {contestant_type: 'SOLO'});
  });

  await t.test('declineRequestValues clears only values equal to the request', () => {
    const values = {
      contestant_type: 'TEAM',
      min_players_in_team: '3',
      max_players_in_team: '5',
      max_teams: ' 16 ',
      reqMap: 'applied',
    };
    const d = rules.declineRequestValues(values, team);
    assert.strictEqual(d.reqMap, 'declined');
    assert.strictEqual(d.min_players_in_team, '');
    assert.strictEqual(d.max_players_in_team, '5');
    assert.strictEqual(d.max_teams, '');
    assert.strictEqual(values.max_teams, ' 16 ');

    const s = rules.declineRequestValues({contestant_type: 'SOLO', max_players: '20'}, solo);
    assert.strictEqual(s.max_players, '20');
  });
});

function summaryOf(i, values) {
  return rules.stepSummary(i, values).map((part) =>
    Object.hasOwn(part, 'text')
      ? part.text
      : rules.format(part.msgid, part.params)
  );
}

test('stepSummary of the competition step names the choices', () => {
  assert.deepStrictEqual(
    summaryOf(1, {
      contestant_type: 'TEAM',
      game_format: 'FREE_FOR_ALL',
      elimination_mode: 'DOUBLE_ELIMINATION',
    }),
    ['Teams', 'Free-for-All', 'Double knockout']
  );
  assert.deepStrictEqual(
    summaryOf(1, {
      contestant_type: 'SOLO',
      game_format: 'ONE_V_ONE',
      elimination_mode: 'ROUND_ROBIN',
    }),
    ['Solo', '1v1', 'Everyone plays everyone']
  );
});

test('stepSummary of the competition step hides the mode for Highscore', () => {
  assert.deepStrictEqual(
    summaryOf(1, {
      contestant_type: 'SOLO',
      game_format: 'HIGHSCORE',
      elimination_mode: 'NONE',
    }),
    ['Solo', 'Highscore']
  );
  assert.deepStrictEqual(
    summaryOf(1, {
      contestant_type: 'SOLO',
      game_format: 'HIGHSCORE',
      elimination_mode: 'SINGLE_ELIMINATION',
    }),
    ['Solo', 'Highscore']
  );
});

test('stepSummary ignores values that are no known choice', () => {
  assert.deepStrictEqual(
    summaryOf(1, {contestant_type: 'constructor', game_format: null}),
    []
  );
});

test('stepSummary of the participants step gives the ranges', () => {
  assert.deepStrictEqual(
    summaryOf(2, {contestant_type: 'SOLO', min_players: '4', max_players: '16'}),
    ['4 to 16 players']
  );
  assert.deepStrictEqual(
    summaryOf(2, {
      contestant_type: 'TEAM',
      min_teams: '8',
      max_teams: '24',
      min_players_in_team: '2',
      max_players_in_team: '3',
    }),
    ['8 to 24 teams', '2 to 3 per team']
  );
  assert.deepStrictEqual(
    summaryOf(2, {contestant_type: 'SOLO', min_players: '', max_players: '32'}),
    ['– to 32 players']
  );
  assert.deepStrictEqual(
    summaryOf(2, {contestant_type: 'SOLO', min_players: '4', max_players: ''}),
    ['4 to ∞ players']
  );
});

test('stepSummary of the participants step is empty without limits', () => {
  assert.deepStrictEqual(
    summaryOf(2, {contestant_type: 'SOLO', min_players: '', max_players: ''}),
    []
  );
  assert.deepStrictEqual(summaryOf(2, {contestant_type: null}), []);
});

test('stepSummary of the scoring step depends on the format', () => {
  assert.deepStrictEqual(summaryOf(3, {game_format: 'ONE_V_ONE'}), [
    'Not needed for 1v1',
  ]);
  assert.deepStrictEqual(summaryOf(3, {game_format: 'HIGHSCORE'}), [
    'Score sorting',
  ]);
  assert.deepStrictEqual(summaryOf(3, {game_format: 'FREE_FOR_ALL'}), [
    'Points, groups',
  ]);
  assert.deepStrictEqual(summaryOf(3, {game_format: null}), [
    'Depends on the format',
  ]);
});

test('stepSummary of the name step and the review step', () => {
  assert.deepStrictEqual(summaryOf(0, {name: '  Cup  '}), ['Cup']);
  assert.deepStrictEqual(summaryOf(0, {name: '  '}), []);
  assert.deepStrictEqual(summaryOf(5, {game_format: 'ONE_V_ONE'}), []);
});

function subtitleOf(status, errorCount, summary, compact) {
  return rules.stepSubtitle(status, errorCount, summary, compact).map((part) =>
    Object.hasOwn(part, 'text')
      ? part.text
      : rules.format(part.msgid, part.params)
  );
}

test('stepSubtitle puts the error count in front of the summary', () => {
  assert.deepStrictEqual(
    subtitleOf('err', 1, [{text: 'Blitzschach'}], false),
    ['1 error', 'Blitzschach']
  );
  assert.deepStrictEqual(
    subtitleOf('err', 2, [{msgid: 'Score sorting'}], false),
    ['2 errors', 'Score sorting']
  );
});

test('stepSubtitle prints nothing for a step without a summary', () => {
  assert.deepStrictEqual(subtitleOf('err', 1, [], false), []);
  assert.deepStrictEqual(subtitleOf('done', 0, [], false), []);
});

test('stepSubtitle shows the summary of a step that is not an error', () => {
  assert.deepStrictEqual(
    subtitleOf('done', 0, [{text: 'Solo'}], false),
    ['Solo']
  );
  assert.deepStrictEqual(
    subtitleOf('cur', 0, [{text: 'Solo'}], false),
    ['Solo']
  );
});

test('stepSubtitle hides the summary of a step that is still open', () => {
  assert.deepStrictEqual(subtitleOf('todo', 0, [{text: 'Solo'}], false), []);
});

test('stepSubtitle of the compact list is only the error count', () => {
  assert.deepStrictEqual(
    subtitleOf('err', 1, [{text: 'Blitzschach'}], true),
    ['1 error']
  );
  assert.deepStrictEqual(subtitleOf('done', 0, [{text: 'Solo'}], true), []);
});

test('point values are bounded in absolute value', async (t) => {
  const ffa = (points, ctx) => errorsOf({
    contestant_type: 'SOLO',
    game_format: 'FREE_FOR_ALL',
    elimination_mode: 'SINGLE_ELIMINATION',
    point_table: points,
    group_size_max: '4',
  }, ctx).point_table;

  await t.test('above the ceiling', () => {
    const error = ffa(['10', '1000000000']);
    assert.strictEqual(error.msgid, 'Points may be at most %(max)s.');
    assert.deepStrictEqual(error.params, {max: 999999999});
  });

  await t.test('far above the ceiling', () => {
    assert.ok(ffa(['1000000000000']));
    assert.ok(ffa('10,1e25'));
  });

  await t.test('below the floor', () => {
    const error = ffa(['10', '-1000000000']);
    assert.strictEqual(error.msgid, 'Points may be at least %(min)s.');
    assert.deepStrictEqual(error.params, {min: -999999999});
    assert.strictEqual(
      ffa('10,-1e25').msgid, 'Points may be at least %(min)s.'
    );
  });

  await t.test('a value above the ceiling wins over one below the floor', () => {
    const error = ffa(['-1000000000', '1000000000']);
    assert.strictEqual(error.msgid, 'Points may be at most %(max)s.');
  });

  await t.test('a value too long for a number is out of range', () => {
    const error = ffa(['10', '9'.repeat(309)]);
    assert.strictEqual(error.msgid, 'Points may be at most %(max)s.');
    assert.strictEqual(
      ffa(['-' + '9'.repeat(400)]).msgid, 'Points may be at least %(min)s.'
    );
    assert.ok(ffa(['1'.padEnd(1000, '0')]));
  });

  await t.test('leading zeros and signs do not change the comparison', () => {
    assert.strictEqual(ffa(['0000000000999999999', '+5']), undefined);
    assert.ok(ffa(['0001000000000']));
  });

  await t.test('at the ceiling is valid', () => {
    assert.strictEqual(ffa(['999999999', '0', '-999999999']), undefined);
  });

  await t.test('the limit comes from the context', () => {
    const limits = {countMax: 1024, pointTableMax: 64, pointValueMax: 100};
    assert.ok(ffa(['101'], {limits}));
    assert.strictEqual(ffa(['100'], {limits}), undefined);
  });

  await t.test('too many places win over too high values', () => {
    const error = ffa(Array(65).fill('1000000000000'));
    assert.strictEqual(error.msgid, 'At most %(max)s places.');
  });
});

test('reviewBanner shows the pre-check result unless a refusal stands', () => {
  for (const status of ['pending', 'offline', 'ok']) {
    assert.strictEqual(rules.reviewBanner({status}, false), status);
    assert.strictEqual(rules.reviewBanner({status}, true), null);
  }
  assert.strictEqual(rules.reviewBanner({status: 'errors'}, false), null);
  assert.strictEqual(rules.reviewBanner(null, false), null);
});

test('startParts reads what the input reports and what the server echoes', () => {
  const expected = {year: '2026', month: '10', day: '17', time: '14:00'};
  assert.deepStrictEqual(rules.startParts('2026-10-17T14:00'), expected);
  assert.deepStrictEqual(rules.startParts('2026-10-17 14:00:00'), expected);
  assert.deepStrictEqual(rules.startParts(' 2026-10-17T14:00 '), expected);
  assert.strictEqual(rules.startParts(''), null);
  assert.strictEqual(rules.startParts('2026-10-17'), null);
  assert.strictEqual(rules.startParts(undefined), null);
});

test('summaryLabel names a choice with its drafted label', () => {
  assert.deepStrictEqual(rules.summaryLabel('SINGLE_ELIMINATION'), [{msgid: 'Single knockout'}]);
  assert.deepStrictEqual(rules.summaryLabel('ROUND_ROBIN'), [{msgid: 'Everyone plays everyone'}]);
  assert.deepStrictEqual(rules.summaryLabel('TEAM'), [{msgid: 'Teams'}]);
  assert.deepStrictEqual(rules.summaryLabel('NONE'), []);
  assert.deepStrictEqual(rules.summaryLabel(null), []);
});

test('modeArea picks the view and the available cards per game format', async (t) => {
  const area = (format) => rules.modeArea(format, VALID_COMBINATIONS);
  const available = (format) => area(format).modes
    .filter((m) => m.available).map((m) => m.mode);

  await t.test('no format shows the hint', () => {
    assert.strictEqual(area(null).view, 'hint');
    assert.strictEqual(area('').view, 'hint');
  });

  await t.test('Highscore shows the fixed row', () => {
    assert.strictEqual(area('HIGHSCORE').view, 'fixed');
    assert.deepStrictEqual(available('HIGHSCORE'), ['NONE']);
  });

  await t.test('1v1 offers three cards, all available', () => {
    assert.strictEqual(area('ONE_V_ONE').view, 'cards');
    assert.deepStrictEqual(available('ONE_V_ONE'), [
      'SINGLE_ELIMINATION', 'DOUBLE_ELIMINATION', 'ROUND_ROBIN',
    ]);
  });

  await t.test('Free-for-All keeps Round Robin as an unavailable card', () => {
    assert.deepStrictEqual(available('FREE_FOR_ALL'), [
      'SINGLE_ELIMINATION', 'DOUBLE_ELIMINATION',
    ]);
    const robin = area('FREE_FOR_ALL').modes.find((m) => m.mode === 'ROUND_ROBIN');
    assert.deepStrictEqual(robin, {mode: 'ROUND_ROBIN', offered: true, available: false});
  });

  await t.test('No knockout is never offered as a card', () => {
    for (const format of ['ONE_V_ONE', 'FREE_FOR_ALL', 'HIGHSCORE']) {
      const none = area(format).modes.find((m) => m.mode === 'NONE');
      assert.strictEqual(none.offered, false, format);
    }
  });

  await t.test('without a combination table everything stays available', () => {
    assert.ok(rules.modeArea('FREE_FOR_ALL', null).modes.every((m) => m.available));
  });
});

// ---------------------------------------------------------------------- //
// Playoffs step
// ---------------------------------------------------------------------- //

const RR_PLAYOFFS = {
  contestant_type: 'SOLO',
  game_format: 'ONE_V_ONE',
  elimination_mode: 'ROUND_ROBIN',
  min_players: '12',
  max_players: '12',
  playoff_enabled: true,
  playoff_group_count: '3',
  playoff_qualifiers_per_group: '2',
  playoff_elimination_mode: 'SINGLE_ELIMINATION',
  playoff_release_mode: 'MANUAL',
};

const HS_PLAYOFFS = {
  contestant_type: 'SOLO',
  game_format: 'HIGHSCORE',
  elimination_mode: 'NONE',
  score_ordering: 'HIGHER_IS_BETTER',
  max_players: '48',
  playoff_enabled: true,
  playoff_qualifier_count: '16',
  playoff_elimination_mode: 'DOUBLE_ELIMINATION',
  playoff_release_mode: 'AUTOMATIC',
  point_table: '10, 7, 5, 3',
  group_size_min: '3',
  group_size_max: '4',
  advancement_count: '2',
};

function msgids(values, ctx) {
  const errors = errorsOf(values, ctx);
  return Object.fromEntries(
    Object.entries(errors).map(([field, msg]) => [field, msg.msgid])
  );
}

test('the playoffs step is the sixth and holds the playoff fields', () => {
  assert.strictEqual(rules.STEP_FIELDS.length, 6);
  assert.deepStrictEqual(rules.STEP_FIELDS[4], [
    'playoff_enabled',
    'playoff_group_count',
    'playoff_qualifiers_per_group',
    'playoff_qualifier_count',
    'playoff_elimination_mode',
    'playoff_release_mode',
  ]);
  assert.deepStrictEqual(rules.STEP_FIELDS[5], ['from_request_id', 'submission_token']);
});

test('stepFields and stepOf move the highscore free-for-all fields', () => {
  const hs = {game_format: 'HIGHSCORE'};
  const ffa = {game_format: 'FREE_FOR_ALL'};
  for (const field of [
    'point_table', 'group_size_min', 'group_size_max', 'advancement_count',
    'points_carry_to_losers',
  ]) {
    assert.strictEqual(rules.stepOf(field, hs), 4, field);
    assert.strictEqual(rules.stepOf(field, ffa), 3, field);
    assert.strictEqual(rules.stepOf(field, {}), 3, field);
    assert.ok(!rules.stepFields(3, hs).includes(field), field);
    assert.ok(rules.stepFields(4, hs).includes(field), field);
  }
  assert.strictEqual(rules.stepOf('score_ordering', hs), 3);
  assert.strictEqual(rules.stepOf('playoff_release_mode', ffa), 4);
  assert.strictEqual(rules.stepOf('submission_token', hs), 5);
  assert.strictEqual(rules.stepOf('no_such_field', hs), -1);
  assert.deepStrictEqual(rules.stepFields(4, ffa), rules.STEP_FIELDS[4]);
});

test('playoff scopes follow the format and the switch', () => {
  const scopes = (values) => rules.applicableScopes(values);

  const none = scopes({game_format: 'ONE_V_ONE', elimination_mode: 'SINGLE_ELIMINATION'});
  assert.strictEqual(none.playoffNone, true);
  assert.strictEqual(none.playoffEligible, false);

  const rrOff = scopes(Object.assign({}, RR_PLAYOFFS, {playoff_enabled: false}));
  assert.strictEqual(rrOff.playoffEligible, true);
  assert.strictEqual(rrOff.playoffKindRr, true);
  assert.strictEqual(rrOff.playoffRr, false);
  assert.strictEqual(rrOff.playoffOn, false);

  const rr = scopes(RR_PLAYOFFS);
  assert.strictEqual(rr.playoffRr, true);
  assert.strictEqual(rr.playoffHs, false);
  assert.strictEqual(rr.playoffOn, true);
  assert.strictEqual(rr.ffa, false);

  const hs = scopes(HS_PLAYOFFS);
  assert.strictEqual(hs.playoffHs, true);
  assert.strictEqual(hs.playoffRr, false);
  // A highscore playoff phase is a Free-for-All phase ...
  assert.strictEqual(hs.ffa, true);
  assert.strictEqual(hs.ffaPlayoff, true);
  assert.strictEqual(hs.ffaDe, true);
  // ... but the sample group of the scoring step stays format-only.
  assert.strictEqual(hs.ffaFormat, false);

  const hsSingle = scopes(Object.assign({}, HS_PLAYOFFS, {playoff_elimination_mode: 'SINGLE_ELIMINATION'}));
  assert.strictEqual(hsSingle.ffaDe, false);

  const hsOff = scopes(Object.assign({}, HS_PLAYOFFS, {playoff_enabled: false}));
  assert.strictEqual(hsOff.ffa, false);
  assert.strictEqual(hsOff.ffaPlayoff, false);
  assert.strictEqual(hsOff.ffaDe, false);

  // A stale switch on a format without playoffs does nothing.
  const stale = scopes({game_format: 'FREE_FOR_ALL', elimination_mode: 'SINGLE_ELIMINATION', playoff_enabled: true});
  assert.strictEqual(stale.playoffOn, false);
  assert.strictEqual(stale.ffaPlayoff, false);
});

test('a valid playoff setup has no errors', () => {
  assert.deepStrictEqual(msgids(RR_PLAYOFFS), {});
  assert.deepStrictEqual(msgids(HS_PLAYOFFS), {});
});

test('the playoff rules only run with the switch on', () => {
  const off = Object.assign({}, RR_PLAYOFFS, {
    playoff_enabled: false,
    playoff_group_count: '1',
    playoff_elimination_mode: null,
    playoff_release_mode: null,
  });
  assert.deepStrictEqual(msgids(off), {});
  const plain = Object.assign({}, RR_PLAYOFFS, {
    elimination_mode: 'SINGLE_ELIMINATION',
    playoff_group_count: '1',
  });
  assert.deepStrictEqual(msgids(plain), {});
});

test('round robin playoff rules', async (t) => {
  const check = (over) => msgids(Object.assign({}, RR_PLAYOFFS, over));

  await t.test('mode and release are required', () => {
    assert.deepStrictEqual(check({playoff_elimination_mode: null, playoff_release_mode: null}), {
      playoff_elimination_mode: 'Please choose a playoff elimination mode.',
      playoff_release_mode: 'Please choose how the playoffs are released.',
    });
  });

  await t.test('groups and qualifiers are required', () => {
    assert.deepStrictEqual(check({playoff_group_count: '', playoff_qualifiers_per_group: ''}), {
      playoff_group_count: 'Please enter the number of groups.',
      playoff_qualifiers_per_group: 'Please enter how many advance from each group.',
    });
  });

  await t.test('a single group is valid', () => {
    assert.deepStrictEqual(check({playoff_group_count: '1'}), {});
  });

  await t.test('a single group needs two qualifiers in total', () => {
    assert.deepStrictEqual(check({playoff_group_count: '1', playoff_qualifiers_per_group: '1'}), {
      playoff_qualifiers_per_group: 'Playoffs need at least 2 qualifiers in total.',
    });
  });

  await t.test('a count must be a whole number from 1', () => {
    assert.strictEqual(check({playoff_group_count: '0'}).playoff_group_count, 'Whole numbers from 1 only.');
    assert.strictEqual(check({playoff_qualifiers_per_group: 'x'}).playoff_qualifiers_per_group, 'Whole numbers from 1 only.');
    assert.strictEqual(check({playoff_group_count: '99999'}).playoff_group_count, 'At most %(max)s.');
  });

  await t.test('the group count stops at the seed code limit', () => {
    const over = errorsOf(Object.assign({}, RR_PLAYOFFS, {min_players: '', max_players: '', playoff_group_count: '256'}));
    assert.strictEqual(over.playoff_group_count.msgid, 'At most %(max)s.');
    assert.deepStrictEqual(over.playoff_group_count.params, {max: 255});
    assert.deepStrictEqual(check({min_players: '', max_players: '', playoff_group_count: '255'}), {});
  });

  await t.test('every group needs two contestants at the maximum', () => {
    assert.deepStrictEqual(check({min_players: '', max_players: '6', playoff_group_count: '4'}), {
      playoff_group_count: 'The maximum number of contestants is too small for this many groups.',
    });
    assert.deepStrictEqual(check({min_players: '', max_players: '6', playoff_group_count: '3'}), {});
    assert.deepStrictEqual(check({
      contestant_type: 'TEAM', min_teams: '', max_teams: '6', min_players: '', max_players: '',
      playoff_group_count: '4',
    }), {playoff_group_count: 'The maximum number of contestants is too small for this many groups.'});
  });

  await t.test('every group needs two contestants at the minimum', () => {
    assert.deepStrictEqual(check({min_players: '5', playoff_group_count: '3'}), {
      playoff_group_count: 'The minimum number of contestants is too small for this many groups.',
    });
  });

  await t.test('fewer advance than the smallest group holds', () => {
    assert.deepStrictEqual(check({min_players: '12', playoff_group_count: '3', playoff_qualifiers_per_group: '4'}), {
      playoff_qualifiers_per_group: 'Fewer must advance from each group than the smallest group holds.',
    });
  });

  await t.test('the smallest group comes from the team minimum for teams', () => {
    assert.deepStrictEqual(check({
      contestant_type: 'TEAM', min_teams: '6', min_players: '', max_players: '',
      playoff_group_count: '3', playoff_qualifiers_per_group: '2',
    }), {playoff_qualifiers_per_group: 'Fewer must advance from each group than the smallest group holds.'});
  });

  await t.test('double knockout takes the same two qualifiers as single knockout', () => {
    assert.deepStrictEqual(check({
      min_players: '', playoff_elimination_mode: 'DOUBLE_ELIMINATION',
      playoff_group_count: '2', playoff_qualifiers_per_group: '1',
    }), {});
    assert.deepStrictEqual(check({
      min_players: '', playoff_elimination_mode: 'DOUBLE_ELIMINATION',
      playoff_group_count: '1', playoff_qualifiers_per_group: '1',
    }), {playoff_qualifiers_per_group: 'Playoffs need at least 2 qualifiers in total.'});
  });

  await t.test('single knockout takes two qualifiers in total', () => {
    assert.deepStrictEqual(check({
      min_players: '', playoff_group_count: '2', playoff_qualifiers_per_group: '1',
    }), {});
  });

  await t.test('without a minimum the size rules do not run', () => {
    assert.deepStrictEqual(check({min_players: '', max_players: '', playoff_group_count: '50', playoff_qualifiers_per_group: '40'}), {});
  });
});

test('highscore playoff rules', async (t) => {
  const check = (over) => msgids(Object.assign({}, HS_PLAYOFFS, over));

  await t.test('the qualifier count is required and at least two', () => {
    assert.deepStrictEqual(check({playoff_qualifier_count: ''}), {
      playoff_qualifier_count: 'Please enter the number of qualifiers.',
    });
    assert.deepStrictEqual(check({playoff_qualifier_count: '1', group_size_min: ''}), {
      playoff_qualifier_count: 'At least two qualifiers are needed.',
    });
  });

  await t.test('the qualifiers reach the minimum lobby size', () => {
    assert.deepStrictEqual(check({playoff_qualifier_count: '2', group_size_min: '3'}), {
      playoff_qualifier_count: 'Qualifiers must be at least the minimum group size.',
    });
    assert.deepStrictEqual(check({playoff_qualifier_count: '2', group_size_min: ''}), {});
  });

  await t.test('the lobby size stops at the seed code limit', () => {
    const over = check({group_size_max: '256'}).group_size_max;
    assert.strictEqual(over, 'At most %(max)s.');
    assert.strictEqual(check({group_size_max: '255'}).group_size_max, undefined);
  });

  await t.test('the free-for-all rules run on the phase-two fields', () => {
    assert.deepStrictEqual(check({point_table: ''}), {point_table: 'Add points for at least place 1.'});
    assert.deepStrictEqual(check({group_size_max: ''}), {group_size_max: 'Required for Free-for-All.'});
    assert.strictEqual(
      check({group_size_min: '5', group_size_max: '4', playoff_qualifier_count: '16'}).group_size_min,
      'Must not be larger than the max. group size (%(n)s).'
    );
    assert.strictEqual(
      check({advancement_count: '4'}).advancement_count,
      'Fewer than %(n)s must advance from the smallest group (%(n)s players).'
    );
  });

  await t.test('the free-for-all fields are not checked without playoffs', () => {
    assert.deepStrictEqual(check({playoff_enabled: false, point_table: '', group_size_max: ''}), {});
  });

  await t.test('the lobby sizes warn like a free-for-all group does', () => {
    const result = rules.validate(
      basics(Object.assign({}, HS_PLAYOFFS, {point_table: '10'})),
      CTX
    );
    assert.ok(result.warnings.point_table);
  });
});

test('stepSummary of the playoffs step', () => {
  assert.deepStrictEqual(summaryOf(4, {game_format: 'ONE_V_ONE', elimination_mode: 'SINGLE_ELIMINATION'}), ['nothing to set']);
  assert.deepStrictEqual(summaryOf(4, {}), ['nothing to set']);
  assert.deepStrictEqual(summaryOf(4, Object.assign({}, RR_PLAYOFFS, {playoff_enabled: false})), ['No playoffs']);
  assert.deepStrictEqual(summaryOf(4, RR_PLAYOFFS), ['3 groups, top 2 each']);
  assert.deepStrictEqual(
    summaryOf(4, Object.assign({}, RR_PLAYOFFS, {playoff_qualifiers_per_group: '1'})),
    ['3 groups, the best one each']
  );
  assert.deepStrictEqual(summaryOf(4, Object.assign({}, RR_PLAYOFFS, {playoff_group_count: ''})), ['Playoffs']);
  assert.deepStrictEqual(
    summaryOf(4, Object.assign({}, RR_PLAYOFFS, {playoff_group_count: '1'})),
    ['One group, top 2']
  );
  assert.deepStrictEqual(
    summaryOf(4, Object.assign({}, RR_PLAYOFFS, {playoff_group_count: '1', playoff_qualifiers_per_group: '1'})),
    ['One group, the best one']
  );
  assert.deepStrictEqual(summaryOf(4, HS_PLAYOFFS), ['Top 16 of the leaderboard']);
  assert.deepStrictEqual(summaryOf(4, Object.assign({}, HS_PLAYOFFS, {playoff_qualifier_count: 'x'})), ['Playoffs']);
});

function previewOf(values) {
  const segments = rules.playoffPreview(values);
  return segments && segments.map((s) => s.sep + rules.format(s.msgid, s.params)).join('');
}

test('playoffPreview of a round robin phase', async (t) => {
  await t.test('names the groups, the qualifiers and the bracket', () => {
    assert.strictEqual(
      previewOf(RR_PLAYOFFS),
      '12 players · 3 groups of 4 · the best 2 → 6 qualifiers → bracket with 8 places, 2 byes'
    );
  });

  await t.test('counts byes and their absence', () => {
    assert.strictEqual(
      previewOf(Object.assign({}, RR_PLAYOFFS, {max_players: '16', playoff_group_count: '4'})),
      '16 players · 4 groups of 4 · the best 2 → 8 qualifiers → bracket with 8 places, no byes'
    );
    assert.strictEqual(
      previewOf(Object.assign({}, RR_PLAYOFFS, {max_players: '14', playoff_group_count: '7', playoff_qualifiers_per_group: '1'})),
      '14 players · 7 groups of 2 · the best one → 7 qualifiers → bracket with 8 places, 1 bye'
    );
  });

  await t.test('groups of unequal size show their range', () => {
    assert.strictEqual(
      previewOf(Object.assign({}, RR_PLAYOFFS, {max_players: '13'})),
      '13 players · 3 groups of 4 to 5 · the best 2 → 6 qualifiers → bracket with 8 places, 2 byes'
    );
  });

  await t.test('teams are counted as teams', () => {
    assert.strictEqual(
      previewOf(Object.assign({}, RR_PLAYOFFS, {contestant_type: 'TEAM', max_teams: '12', max_players: ''})),
      '12 teams · 3 groups of 4 · the best 2 → 6 qualifiers → bracket with 8 places, 2 byes'
    );
  });

  await t.test('without a maximum the sentence leaves out the sizes', () => {
    assert.strictEqual(
      previewOf(Object.assign({}, RR_PLAYOFFS, {max_players: ''})),
      '3 groups · the best 2 → 6 qualifiers → bracket with 8 places, 2 byes'
    );
  });

  await t.test('one group is named in the singular', () => {
    assert.strictEqual(
      previewOf(Object.assign({}, RR_PLAYOFFS, {playoff_group_count: '1'})),
      '12 players · One group of 12 · the best 2 → 2 qualifiers → bracket with 2 places, no byes'
    );
    assert.strictEqual(
      previewOf(Object.assign({}, RR_PLAYOFFS, {playoff_group_count: '1', max_players: ''})),
      'One group · the best 2 → 2 qualifiers → bracket with 2 places, no byes'
    );
  });

  await t.test('incomplete settings give no preview', () => {
    assert.strictEqual(previewOf(Object.assign({}, RR_PLAYOFFS, {playoff_group_count: ''})), null);
    assert.strictEqual(previewOf(Object.assign({}, RR_PLAYOFFS, {playoff_group_count: '0'})), null);
    assert.strictEqual(previewOf(Object.assign({}, RR_PLAYOFFS, {playoff_enabled: false})), null);
  });
});

test('playoffPreview: double elimination with too few qualifiers', async (t) => {
  const DE = Object.assign({}, RR_PLAYOFFS, {
    playoff_group_count: '2',
    playoff_qualifiers_per_group: '1',
    playoff_elimination_mode: 'DOUBLE_ELIMINATION',
  });

  await t.test('double knockout below four previews the single elimination fallback', () => {
    assert.strictEqual(
      previewOf(DE),
      '12 players · 2 groups of 6 · the best one → 2 qualifiers → bracket with 2 places, no byes · double knockout runs as single knockout (fewer than 4)'
    );
    const segments = rules.playoffPreview(DE);
    assert.strictEqual(
      segments[segments.length - 1].msgid,
      'double knockout runs as single knockout (fewer than 4)'
    );
    assert.strictEqual(segments.some((s) => s.bad), false);
  });

  await t.test('four qualifiers give the normal sentence', () => {
    const four = Object.assign({}, DE, {playoff_qualifiers_per_group: '2'});
    assert.strictEqual(
      previewOf(four),
      '12 players · 2 groups of 6 · the best 2 → 4 qualifiers → bracket with 4 places, no byes'
    );
    assert.strictEqual(rules.playoffPreview(four).some((s) => s.bad), false);
  });

  await t.test('single elimination is not affected', () => {
    const single = Object.assign({}, DE, {playoff_elimination_mode: 'SINGLE_ELIMINATION'});
    assert.match(previewOf(single), /bracket with 2 places, no byes$/);
    assert.doesNotMatch(previewOf(single), /single knockout/);
  });
});

test('playoffPreview of a highscore phase', async (t) => {
  await t.test('names the qualifiers, the lobbies and the advancers', () => {
    assert.strictEqual(
      previewOf(HS_PLAYOFFS),
      'Up to 48 players · the best 16 → 4 lobbies of 4 → each 2 advance'
    );
  });

  await t.test('lobbies of unequal size show their range', () => {
    assert.strictEqual(
      previewOf(Object.assign({}, HS_PLAYOFFS, {playoff_qualifier_count: '10'})),
      'Up to 48 players · the best 10 → 3 lobbies of 3 to 4 → each 2 advance'
    );
  });

  await t.test('without lobby sizes it stops at the qualifiers', () => {
    assert.strictEqual(
      previewOf(Object.assign({}, HS_PLAYOFFS, {group_size_max: '', advancement_count: ''})),
      'Up to 48 players · the best 16'
    );
  });

  await t.test('without a maximum the sentence starts at the qualifiers', () => {
    assert.strictEqual(
      previewOf(Object.assign({}, HS_PLAYOFFS, {max_players: ''})),
      'the best 16 → 4 lobbies of 4 → each 2 advance'
    );
  });

  await t.test('no qualifiers, no preview', () => {
    assert.strictEqual(previewOf(Object.assign({}, HS_PLAYOFFS, {playoff_qualifier_count: ''})), null);
  });
});

test('a change of format resets the playoff settings visibly', async (t) => {
  const ctx = {validCombinations: VALID_COMBINATIONS};

  await t.test('round robin to single knockout', () => {
    const {values, notices} = rules.dependentChange(RR_PLAYOFFS, 'elimination_mode', 'SINGLE_ELIMINATION', ctx);
    assert.strictEqual(values.elimination_mode, 'SINGLE_ELIMINATION');
    assert.strictEqual(values.playoff_enabled, false);
    assert.strictEqual(values.playoff_group_count, '');
    assert.strictEqual(values.playoff_qualifiers_per_group, '');
    assert.strictEqual(values.playoff_elimination_mode, null);
    assert.strictEqual(values.playoff_release_mode, null);
    assert.strictEqual(notices.length, 1);
    assert.strictEqual(notices[0].msgid, 'Playoff settings reset. They do not fit the new format.');
    assert.strictEqual(notices[0].undo.playoff_enabled, true);
    assert.strictEqual(notices[0].undo.playoff_group_count, '3');
    assert.strictEqual(notices[0].undo.elimination_mode, 'ROUND_ROBIN');
  });

  await t.test('highscore to free-for-all', () => {
    const {values, notices} = rules.dependentChange(HS_PLAYOFFS, 'game_format', 'FREE_FOR_ALL', ctx);
    assert.strictEqual(values.playoff_enabled, false);
    assert.strictEqual(values.playoff_qualifier_count, '');
    assert.ok(notices.some((n) => n.msgid.startsWith('Playoff settings reset')));
  });

  await t.test('highscore to round robin', () => {
    const {values, notices} = rules.dependentChange(HS_PLAYOFFS, 'game_format', 'ONE_V_ONE', ctx);
    assert.strictEqual(values.elimination_mode, null);
    assert.strictEqual(values.playoff_enabled, false);
    assert.ok(notices.some((n) => n.undo && n.undo.playoff_qualifier_count === '16'));
  });

  await t.test('a change within the playoff kind keeps the settings', () => {
    const same = rules.dependentChange(RR_PLAYOFFS, 'elimination_mode', 'ROUND_ROBIN', ctx);
    assert.strictEqual(same.values.playoff_enabled, true);
    assert.deepStrictEqual(same.notices, []);
  });

  await t.test('nothing to reset without playoff settings', () => {
    const plain = {game_format: 'ONE_V_ONE', elimination_mode: 'ROUND_ROBIN', playoff_enabled: false};
    const {values, notices} = rules.dependentChange(plain, 'elimination_mode', 'SINGLE_ELIMINATION', ctx);
    assert.strictEqual(values.elimination_mode, 'SINGLE_ELIMINATION');
    assert.deepStrictEqual(notices, []);
  });

  await t.test('a round robin switch from single knockout starts without settings', () => {
    const {values, notices} = rules.dependentChange(
      {game_format: 'ONE_V_ONE', elimination_mode: 'SINGLE_ELIMINATION', playoff_enabled: true},
      'elimination_mode', 'ROUND_ROBIN', ctx
    );
    assert.strictEqual(values.elimination_mode, 'ROUND_ROBIN');
    assert.deepStrictEqual(notices, []);
  });
});

test('ffaCutMissing mirrors the server table', () => {
  const ffa = {
    contestant_type: 'SOLO',
    game_format: 'FREE_FOR_ALL',
    elimination_mode: 'SINGLE_ELIMINATION',
  };
  const hs = {
    contestant_type: 'SOLO',
    game_format: 'HIGHSCORE',
    elimination_mode: null,
    playoff_enabled: true,
    playoff_elimination_mode: 'SINGLE_ELIMINATION',
  };
  const counts = (extra) => Object.assign({
    max_players: null, max_teams: null, playoff_qualifier_count: null,
    advancement_count: null, group_size_max: 4,
  }, extra);
  const de = {elimination_mode: 'DOUBLE_ELIMINATION'};

  // prettier-ignore
  const rows = [
    ['de-single-lobby', Object.assign({}, ffa, de), counts({max_players: 4}), true],
    ['se-no-bound', ffa, counts(), true],
    ['se-bound-above-lobby', ffa, counts({max_players: 8}), true],
    ['se-bound-in-lobby', ffa, counts({max_players: 4}), false],
    ['team-bound-above-lobby', Object.assign({}, ffa, {contestant_type: 'TEAM'}), counts({max_teams: 8}), true],
    ['team-bound-in-lobby', Object.assign({}, ffa, {contestant_type: 'TEAM'}), counts({max_teams: 4}), false],
    ['team-no-bound', Object.assign({}, ffa, {contestant_type: 'TEAM'}), counts(), true],
    ['playoff-above-lobby', hs, counts({playoff_qualifier_count: 8}), true],
    ['playoff-in-lobby', hs, counts({playoff_qualifier_count: 4}), false],
    ['playoff-de-single-lobby', Object.assign({}, hs, {playoff_elimination_mode: 'DOUBLE_ELIMINATION'}), counts({playoff_qualifier_count: 4}), true],
    ['cut-set', ffa, counts({advancement_count: 1}), false],
    ['no-group-max', ffa, counts({group_size_max: null}), false],
    ['non-ffa', Object.assign({}, ffa, {game_format: 'ONE_V_ONE'}), counts(), false],
    ['highscore-without-playoff', Object.assign({}, hs, {playoff_enabled: false}), counts({playoff_qualifier_count: 8}), false],
  ];
  for (const [name, values, c, expected] of rows) {
    assert.strictEqual(rules.ffaCutMissing(values, c), expected, name);
  }
});
