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
  return Object.assign({name: 'Cup'}, values);
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

  await t.test('1v1 skips step 4 in both directions', () => {
    const v = {game_format: 'ONE_V_ONE'};
    assert.strictEqual(rules.skipScoring(v), true);
    assert.strictEqual(rules.nextStep(2, v), 4);
    assert.strictEqual(rules.prevStep(4, v), 2);
    assert.strictEqual(rules.nextStep(1, v), 2);
    assert.strictEqual(rules.prevStep(3, v), 2);
  });

  await t.test('other formats visit every step', () => {
    const v = {game_format: 'FREE_FOR_ALL'};
    assert.strictEqual(rules.skipScoring(v), false);
    assert.strictEqual(rules.nextStep(2, v), 3);
    assert.strictEqual(rules.prevStep(4, v), 3);
    assert.strictEqual(rules.nextStep(4, v), 4);
    assert.strictEqual(rules.prevStep(0, v), 0);
  });

  await t.test('cur, done, err, skip, todo', () => {
    const s = state({visited: {0: true, 1: true, 2: true}, values: {game_format: 'ONE_V_ONE'}});
    assert.strictEqual(rules.stepStatus(1, s, {}), 'cur');
    assert.strictEqual(rules.stepStatus(0, s, {}), 'done');
    assert.strictEqual(rules.stepStatus(0, s, {name: {msgid: 'x'}}), 'err');
    assert.strictEqual(rules.stepStatus(2, s, {min_players: {msgid: 'x'}}), 'err');
    assert.strictEqual(rules.stepStatus(3, s, {}), 'skip');
    assert.strictEqual(rules.stepStatus(4, s, {}), 'todo');
  });

  await t.test('skip needs a visited step 3', () => {
    const s = state({visited: {}, values: {game_format: 'ONE_V_ONE'}});
    assert.strictEqual(rules.stepStatus(3, s, {}), 'todo');
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
  assert.deepStrictEqual(s, {solo: false, team: true, highscore: false, ffa: true, ffaDe: true, oneVOne: false});
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
  assert.deepStrictEqual(summaryOf(4, {game_format: 'ONE_V_ONE'}), []);
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
