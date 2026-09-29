/*
 * DOM-free rules of the tournament creation wizard.
 *
 * This is convenience only. The server (`validate_tournament_settings` and
 * the create form) is the actual gate: every rule here has a server twin
 * and the final POST re-validates everything.
 *
 * Messages are English msgids plus params. The caller translates them.
 * Params that name a label (`other`, `mode`, `format`, `type`) hold the
 * English label msgid and need translating too.
 *
 * Node: `module.exports`. Browser: the global `LtCreateWizardRules`.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) {
    module.exports = factory();
  } else {
    root.LtCreateWizardRules = factory();
  }
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var STEP_FIELDS = [
    [
      'name', 'game', 'start_time', 'description', 'ruleset', 'image',
      'image_id', 'image_url', 'image_alt_text'
    ],
    ['contestant_type', 'game_format', 'elimination_mode'],
    [
      'req_map', 'min_players', 'max_players', 'min_teams', 'max_teams',
      'min_players_in_team', 'max_players_in_team'
    ],
    [
      'score_ordering', 'point_table', 'group_size_min', 'group_size_max',
      'advancement_count', 'points_carry_to_losers'
    ],
    ['from_request_id', 'submission_token']
  ];

  var LAST_STEP = STEP_FIELDS.length - 1;

  var DEFAULT_LIMITS = {
    nameMax: 80,
    gameMax: 80,
    textMax: 10000,
    pointTableMax: 64,
    pointValueMax: 999999999,
    countMax: 1024
  };

  var TYPE_LABELS = {SOLO: 'Solo', TEAM: 'Teams'};
  var FORMAT_LABELS = {
    ONE_V_ONE: '1v1',
    FREE_FOR_ALL: 'Free-for-All',
    HIGHSCORE: 'Highscore'
  };
  var MODE_LABELS = {
    SINGLE_ELIMINATION: 'Single knockout',
    DOUBLE_ELIMINATION: 'Double knockout',
    ROUND_ROBIN: 'Everyone plays everyone',
    NONE: 'No knockout'
  };
  var FIELD_LABELS = {
    min_players: 'Min. players',
    max_players: 'Max. players',
    min_teams: 'Min. teams',
    max_teams: 'Max. teams',
    min_players_in_team: 'Min. players per team',
    max_players_in_team: 'Max. players per team'
  };
  var SUMMARY_LABELS = {
    SOLO: 'Solo',
    TEAM: 'Teams',
    ONE_V_ONE: '1v1',
    FREE_FOR_ALL: 'Free-for-All',
    HIGHSCORE: 'Highscore',
    SINGLE_ELIMINATION: 'Single knockout',
    DOUBLE_ELIMINATION: 'Double knockout',
    ROUND_ROBIN: 'Everyone plays everyone'
  };
  var SOLO_COUNTS = ['min_players', 'max_players'];
  var TEAM_COUNTS = [
    'min_teams', 'max_teams', 'min_players_in_team', 'max_players_in_team'
  ];
  var FFA_FIELDS = [
    'group_size_min', 'group_size_max', 'advancement_count'
  ];

  var MSG_CHOOSE_TYPE = 'Please choose whether individuals or teams compete.';
  var MSG_CHOOSE_FORMAT = 'Please choose a game format.';
  var MSG_CHOOSE_MODE = 'Please choose an elimination mode.';
  var MSG_BAD_COMBINATION =
    'This combination of game format and elimination mode is not supported.';
  var MSG_MIN_ABOVE_MAX = 'Must be at least "%(other)s" (%(n)s).';
  var MSG_CHOOSE_ORDERING = 'Please choose which results are better.';
  var MSG_WHOLE = 'Whole numbers from 1 only.';
  var MSG_AT_LEAST_2 = 'At least 2.';
  var MSG_AT_MOST = 'At most %(max)s.';
  var MSG_POINTS_TOO_HIGH = 'Points may be at most %(max)s.';
  var MSG_POINTS_TOO_LOW = 'Points may be at least %(min)s.';
  var MSG_LENGTH = 'At most %(max)d characters – currently %(length)d.';

  function format(msg, params) {
    params = params || {};
    return String(msg).replace(
      /%%|%\((\w+)\)[sd]/g,
      function (whole, key) {
        if (whole === '%%') {
          return '%';
        }
        return Object.prototype.hasOwnProperty.call(params, key)
          ? String(params[key])
          : whole;
      }
    );
  }

  function isBlank(value) {
    return value === undefined || value === null || String(value).trim() === '';
  }

  function limitsOf(ctx) {
    var given = (ctx && ctx.limits) || {};
    var limits = {};
    Object.keys(DEFAULT_LIMITS).forEach(function (key) {
      limits[key] = given[key] !== undefined ? given[key] : DEFAULT_LIMITS[key];
    });
    return limits;
  }

  function pointTableList(values) {
    var raw = values.point_table;
    if (raw === undefined || raw === null) {
      return [];
    }
    var items = Array.isArray(raw) ? raw : String(raw).split(',');
    return items
      .map(function (item) { return String(item).trim(); })
      .filter(function (item) { return item !== ''; });
  }

  // Whole numbers are compared digit by digit, so that no length is parsed
  // to Infinity and passes.
  // 1 above `max`, -1 below `-max`, 0 in between or not a number.
  function outOfRange(item, max) {
    var text = String(item).trim();
    if (/^[+-]?\d+$/.test(text)) {
      var digits = text.replace(/^[+-]/, '').replace(/^0+(?=\d)/, '');
      var ceiling = String(Math.abs(max));
      var beyond = digits.length > ceiling.length ||
        (digits.length === ceiling.length && digits > ceiling);
      return beyond ? (text.charAt(0) === '-' ? -1 : 1) : 0;
    }
    var n = Number(text);
    if (isNaN(n) || Math.abs(n) <= max) {
      return 0;
    }
    return n < 0 ? -1 : 1;
  }

  function charLength(text) {
    return Array.from(text).length;
  }

  function message(msgid, params) {
    return {msgid: msgid, params: params || {}};
  }

  function parseCount(raw, min, countMax) {
    if (isBlank(raw)) {
      return {value: null, error: null};
    }
    if (!/^\s*\d+\s*$/.test(String(raw))) {
      return {value: null, error: message(MSG_WHOLE)};
    }
    var n = parseInt(raw, 10);
    if (n < min) {
      return {
        value: null,
        error: message(min > 1 ? MSG_AT_LEAST_2 : MSG_WHOLE)
      };
    }
    if (n > countMax) {
      return {value: null, error: message(MSG_AT_MOST, {max: countMax})};
    }
    return {value: n, error: null};
  }

  function isValidUrl(text) {
    return /^https?:\/\/[^\s\/?#]+[^\s]*$/i.test(text);
  }

  function validate(values, ctx) {
    values = values || {};
    ctx = ctx || {};
    var limits = limitsOf(ctx);
    var requireStructure = ctx.requireStructure !== false;
    var errors = {};
    var warnings = {};

    function add(field, msgid, params) {
      if (!Object.prototype.hasOwnProperty.call(errors, field)) {
        errors[field] = message(msgid, params);
      }
    }

    // Client-side form rules (twins of the create form's validators).
    var name = isBlank(values.name) ? '' : String(values.name).trim();
    if (name === '') {
      add('name', 'Please enter a name.');
    } else if (charLength(name) > limits.nameMax) {
      add('name', MSG_LENGTH, {
        max: limits.nameMax,
        length: charLength(name)
      });
    }
    [
      ['game', limits.gameMax],
      ['description', limits.textMax],
      ['ruleset', limits.textMax]
    ].forEach(function (rule) {
      var text = values[rule[0]];
      if (!isBlank(text) && charLength(String(text)) > rule[1]) {
        add(rule[0], MSG_LENGTH, {
          max: rule[1],
          length: charLength(String(text))
        });
      }
    });

    var url = isBlank(values.image_url) ? '' : String(values.image_url).trim();
    if (url !== '' && values.urlMode !== false && !isValidUrl(url)) {
      add(
        'image_url',
        'Only complete http or https addresses, ' +
          'e.g. https://example.org/image.png'
      );
    }

    var counts = {};
    [
      ['min_players', 1], ['max_players', 1], ['min_teams', 1],
      ['max_teams', 1], ['min_players_in_team', 1],
      ['max_players_in_team', 1], ['group_size_min', 2],
      ['group_size_max', 2], ['advancement_count', 1]
    ].forEach(function (rule) {
      var parsed = parseCount(values[rule[0]], rule[1], limits.countMax);
      counts[rule[0]] = parsed.value;
      if (parsed.error) {
        add(rule[0], parsed.error.msgid, parsed.error.params);
      }
    });

    if (ctx.fromRequest && values.contestant_type && !values.reqMap) {
      add(
        'req_map',
        'Please decide whether to apply the participant values from the ' +
          'request.'
      );
    }

    // Domain rules: the twin of `validate_tournament_settings`.
    var contestantType = values.contestant_type || null;
    var gameFormat = values.game_format || null;
    var eliminationMode = values.elimination_mode || null;
    var combinations = ctx.validCombinations || null;

    function checkPair(minField, maxField, minLabel) {
      var minValue = counts[minField];
      var maxValue = counts[maxField];
      if (minValue !== null && maxValue !== null && minValue > maxValue) {
        add(maxField, MSG_MIN_ABOVE_MAX, {other: minLabel, n: minValue});
      }
    }

    if (requireStructure) {
      if (!contestantType) {
        add('contestant_type', MSG_CHOOSE_TYPE);
      }
      if (!gameFormat) {
        add('game_format', MSG_CHOOSE_FORMAT);
      }
      if (!eliminationMode && gameFormat !== 'HIGHSCORE') {
        add('elimination_mode', MSG_CHOOSE_MODE);
      }
    }

    if (gameFormat && eliminationMode && combinations) {
      var allowed = combinations[gameFormat] || [];
      if (allowed.indexOf(eliminationMode) === -1) {
        add('elimination_mode', MSG_BAD_COMBINATION);
      }
    }

    if (contestantType === 'SOLO') {
      checkPair('min_players', 'max_players', FIELD_LABELS.min_players);
    } else if (contestantType === 'TEAM') {
      checkPair('min_teams', 'max_teams', FIELD_LABELS.min_teams);
      checkPair(
        'min_players_in_team',
        'max_players_in_team',
        FIELD_LABELS.min_players_in_team
      );
    }

    if (gameFormat === 'HIGHSCORE' && !values.score_ordering) {
      add('score_ordering', MSG_CHOOSE_ORDERING);
    }

    var points = pointTableList(values);
    var groupMin = counts.group_size_min;
    var groupMax = counts.group_size_max;

    if (gameFormat === 'FREE_FOR_ALL') {
      var isTeam = contestantType === 'TEAM';
      var advancement = counts.advancement_count;

      if (points.length === 0) {
        add('point_table', 'Add points for at least place 1.');
      } else if (points.length > limits.pointTableMax) {
        add('point_table', 'At most %(max)s places.', {
          max: limits.pointTableMax
        });
      } else if (points.some(function (item) {
        return outOfRange(item, limits.pointValueMax) > 0;
      })) {
        add('point_table', MSG_POINTS_TOO_HIGH, {max: limits.pointValueMax});
      } else if (points.some(function (item) {
        return outOfRange(item, limits.pointValueMax) < 0;
      })) {
        add('point_table', MSG_POINTS_TOO_LOW, {min: -limits.pointValueMax});
      }

      var groupMinTooLarge = false;
      if (groupMax === null) {
        add('group_size_max', 'Required for Free-for-All.');
      } else if (groupMin !== null && groupMin > groupMax) {
        groupMinTooLarge = true;
        add(
          'group_size_min',
          'Must not be larger than the max. group size (%(n)s).',
          {n: groupMax}
        );
      }

      var maxContestants = isTeam ? counts.max_teams : counts.max_players;
      var need = groupMin !== null ? groupMin : 2;
      if (
        !groupMinTooLarge &&
        maxContestants !== null &&
        maxContestants < need
      ) {
        if (isTeam) {
          add(
            'group_size_min',
            'With at most %(n)s teams no group of at least %(min)s teams ' +
              'can form. Lower the minimum to %(n)s or raise ' +
              '"Max. teams" in step 3.',
            {n: maxContestants, min: need}
          );
        } else {
          add(
            'group_size_min',
            'With at most %(n)s players no group of at least %(min)s ' +
              'can form. Lower the minimum or raise "Max. players" in ' +
              'step 3.',
            {n: maxContestants, min: need}
          );
        }
      }

      if (advancement !== null) {
        var smallest = groupMin !== null && !groupMinTooLarge
          ? groupMin
          : groupMax;
        if (smallest !== null && advancement >= smallest) {
          add(
            'advancement_count',
            isTeam
              ? 'Fewer than %(n)s must advance from the smallest group ' +
                '(%(n)s teams).'
              : 'Fewer than %(n)s must advance from the smallest group ' +
                '(%(n)s players).',
            {n: smallest}
          );
        }
      }
    }

    // Warnings: hints only, never enforced.
    var seats = ctx.capacity;
    if (seats !== undefined && seats !== null) {
      if (contestantType === 'SOLO') {
        if (!errors.max_players && counts.max_players > seats) {
          warnings.max_players = message(
            'More than the party has seats (%(seats)s).',
            {seats: seats}
          );
        }
      } else if (contestantType === 'TEAM') {
        var teams = counts.max_teams;
        var perTeam = counts.max_players_in_team;
        if (
          teams && perTeam && !errors.max_teams &&
          !errors.max_players_in_team && teams * perTeam > seats
        ) {
          warnings.max_teams = message(
            '%(t)s teams × %(p)s players = %(n)s, more than the party ' +
              'has seats (%(seats)s).',
            {t: teams, p: perTeam, n: teams * perTeam, seats: seats}
          );
        }
      }
    }
    if (
      gameFormat === 'FREE_FOR_ALL' && !errors.point_table &&
      !errors.group_size_max && groupMax !== null && points.length < groupMax
    ) {
      var first = points.length + 1;
      warnings.point_table = first < groupMax
        ? message('Places %(from)s–%(to)s get 0 points.', {
            from: first,
            to: groupMax
          })
        : message('Place %(n)s gets 0 points.', {n: first});
    }

    return {errors: errors, warnings: warnings};
  }

  function applicableScopes(values) {
    values = values || {};
    var ffa = values.game_format === 'FREE_FOR_ALL';
    return {
      solo: values.contestant_type === 'SOLO',
      team: values.contestant_type === 'TEAM',
      highscore: values.game_format === 'HIGHSCORE',
      ffa: ffa,
      ffaDe: ffa && values.elimination_mode === 'DOUBLE_ELIMINATION',
      oneVOne: values.game_format === 'ONE_V_ONE'
    };
  }

  var MODES = [
    'SINGLE_ELIMINATION', 'DOUBLE_ELIMINATION', 'ROUND_ROBIN', 'NONE'
  ];

  // What the mode area of step 2 shows for a game format: the hint, the
  // fixed Highscore row or the cards. A card is available when the server
  // accepts the pair. "No knockout" is never a card: Highscore sets it.
  function modeArea(gameFormat, combinations) {
    var allowed = (gameFormat && combinations && combinations[gameFormat]) ||
      [];
    return {
      view: !gameFormat ? 'hint' : gameFormat === 'HIGHSCORE' ? 'fixed' : 'cards',
      modes: MODES.map(function (mode) {
        return {
          mode: mode,
          offered: mode !== 'NONE',
          available: !gameFormat || !combinations ||
            allowed.indexOf(mode) !== -1
        };
      })
    };
  }

  function skipScoring(values) {
    return values.game_format === 'ONE_V_ONE';
  }

  function nextStep(i, values) {
    if (i === 2 && skipScoring(values)) {
      return 4;
    }
    return Math.min(i + 1, LAST_STEP);
  }

  function prevStep(i, values) {
    if (i === 4 && skipScoring(values)) {
      return 2;
    }
    return Math.max(i - 1, 0);
  }

  function wholeNumber(raw) {
    return /^\s*\d+\s*$/.test(String(raw)) ? parseInt(raw, 10) : null;
  }

  function rangeSummary(min, max, msgid) {
    var low = wholeNumber(min);
    var high = wholeNumber(max);
    if (low === null && high === null) {
      return [];
    }
    return [{
      msgid: msgid,
      params: {
        min: low === null ? '–' : low,
        max: high === null ? '∞' : high
      }
    }];
  }

  function summaryLabel(value) {
    return Object.prototype.hasOwnProperty.call(SUMMARY_LABELS, value)
      ? [{msgid: SUMMARY_LABELS[value]}]
      : [];
  }

  // Subtitle of step `i` in the stepper: parts of {text} or {msgid, params}.
  function stepSummary(i, values) {
    if (i === 0) {
      return isBlank(values.name) ? [] : [{text: String(values.name).trim()}];
    }
    if (i === 1) {
      return summaryLabel(values.contestant_type)
        .concat(summaryLabel(values.game_format))
        .concat(
          values.game_format === 'HIGHSCORE'
            ? []
            : summaryLabel(values.elimination_mode)
        );
    }
    if (i === 2) {
      if (values.contestant_type === 'SOLO') {
        return rangeSummary(
          values.min_players, values.max_players, '%(min)s to %(max)s players'
        );
      }
      if (values.contestant_type === 'TEAM') {
        return rangeSummary(
          values.min_teams, values.max_teams, '%(min)s to %(max)s teams'
        ).concat(rangeSummary(
          values.min_players_in_team, values.max_players_in_team,
          '%(min)s to %(max)s per team'
        ));
      }
      return [];
    }
    if (i !== 3) {
      return [];
    }
    if (skipScoring(values)) {
      return [{msgid: 'Not needed for 1v1'}];
    }
    if (values.game_format === 'HIGHSCORE') {
      return [{msgid: 'Score sorting'}];
    }
    if (values.game_format === 'FREE_FOR_ALL') {
      return [{msgid: 'Points, groups'}];
    }
    return [{msgid: 'Depends on the format'}];
  }

  // Parts of a stepper item's subtitle. The list prints the error count in
  // front of the summary and nothing when the step has no summary; the
  // compact list prints only the error count.
  function stepSubtitle(status, errorCount, summary, compact) {
    var errors = status === 'err' && errorCount > 0
      ? [{
        msgid: errorCount === 1 ? '%(n)s error' : '%(n)s errors',
        params: {n: errorCount}
      }]
      : [];
    if (compact) {
      return errors;
    }
    if (status === 'todo' || summary.length === 0) {
      return [];
    }
    return errors.concat(summary);
  }

  // `state`: {step, visited: {index: true}, values}; `errors`: field map.
  function stepStatus(i, state, errors) {
    var visited = state.visited || {};
    errors = errors || {};
    if (i === state.step) {
      return 'cur';
    }
    if (i === 3 && skipScoring(state.values || {}) && visited[2]) {
      return 'skip';
    }
    if (visited[i]) {
      var hasError = STEP_FIELDS[i].some(function (field) {
        return Object.prototype.hasOwnProperty.call(errors, field);
      });
      return hasError ? 'err' : 'done';
    }
    return 'todo';
  }

  function copy(values) {
    var result = {};
    Object.keys(values).forEach(function (key) { result[key] = values[key]; });
    return result;
  }

  function isSet(value) {
    return value !== undefined && value !== null && value !== '';
  }

  function dependentChange(prev, field, value, ctx) {
    var values = copy(prev);
    var notices = [];
    var combinations = (ctx && ctx.validCombinations) || null;

    if (field === 'game_format') {
      var prevFormat = prev.game_format || null;
      var prevMode = prev.elimination_mode || null;
      if (prevFormat === value) {
        return {values: values, notices: notices};
      }
      var restore = {game_format: prevFormat, elimination_mode: prevMode};
      values.game_format = value;

      if (value === 'HIGHSCORE') {
        values.elimination_mode = 'NONE';
      } else if (
        prevMode && combinations &&
        (combinations[value] || []).indexOf(prevMode) === -1
      ) {
        values.elimination_mode = null;
        if (prevMode !== 'NONE') {
          notices.push({
            msgid: 'Elimination mode reset. "%(mode)s" is not available ' +
              'for %(format)s. Please choose again.',
            params: {
              mode: MODE_LABELS[prevMode] || prevMode,
              format: FORMAT_LABELS[value] || value
            },
            undo: restore
          });
        }
      }
      if (
        prevFormat === 'FREE_FOR_ALL' &&
        (pointTableList(prev).length > 0 ||
          FFA_FIELDS.some(function (key) { return isSet(prev[key]); }))
      ) {
        notices.push({
          msgid: 'Free-for-All settings no longer apply. Points, group ' +
            'sizes and advancement are not saved for %(format)s.',
          params: {format: FORMAT_LABELS[value] || value},
          undo: restore
        });
      }
      if (prevFormat === 'HIGHSCORE' && prev.score_ordering) {
        notices.push({
          msgid: 'Score ordering no longer applies. It is only saved for ' +
            'Highscore.',
          params: {},
          undo: restore
        });
      }
      return {values: values, notices: notices.slice(0, 2)};
    }

    if (field === 'contestant_type') {
      var prevType = prev.contestant_type || null;
      if (prevType === value) {
        return {values: values, notices: notices};
      }
      values.contestant_type = value;
      values.reqMap = null;
      var other = prevType === 'SOLO'
        ? SOLO_COUNTS
        : prevType === 'TEAM' ? TEAM_COUNTS : [];
      var filled = other.filter(function (key) { return isSet(prev[key]); });
      if (filled.length) {
        notices.push({
          msgid: 'You switched to %(type)s. %(fields)s only apply to ' +
            '%(other)s and are not saved.',
          params: {
            type: TYPE_LABELS[value] || value,
            other: TYPE_LABELS[prevType] || prevType,
            fields: filled.map(function (key) {
              return {label: FIELD_LABELS[key], value: prev[key]};
            })
          },
          undo: {contestant_type: prevType, reqMap: prev.reqMap || null}
        });
      }
      return {values: values, notices: notices.slice(0, 2)};
    }

    values[field] = value;
    return {values: values, notices: notices};
  }

  // Which pre-check banner the review shows; a standing refusal shows none.
  function reviewBanner(check, refused) {
    if (refused || !check) {
      return null;
    }
    return ['pending', 'offline', 'ok'].indexOf(check.status) !== -1
      ? check.status
      : null;
  }

  // The parts of a start time as the input reports it or the server echoes
  // it (`2026-10-17T14:00`, `2026-10-17 14:00:00`); null when incomplete.
  function startParts(raw) {
    var match = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/.exec(
      String(raw === undefined || raw === null ? '' : raw).trim()
    );
    return match
      ? {
        year: match[1], month: match[2], day: match[3],
        time: match[4] + ':' + match[5]
      }
      : null;
  }

  function initialReqMap(values, request) {
    if (request && values.contestant_type === request.derivedContestantType) {
      return 'applied';
    }
    return null;
  }

  function applyRequestValues(values, request) {
    var result = copy(values);
    result.reqMap = 'applied';
    if (!request) {
      return result;
    }
    if (values.contestant_type === 'TEAM') {
      if (isSet(request.teamSize)) {
        result.min_players_in_team = String(request.teamSize);
        result.max_players_in_team = String(request.teamSize);
      }
      if (isSet(request.participantLimit)) {
        result.max_teams = String(request.participantLimit);
      }
    } else if (values.contestant_type === 'SOLO') {
      if (isSet(request.participantLimit)) {
        result.max_players = String(request.participantLimit);
      }
    }
    return result;
  }

  function declineRequestValues(values, request) {
    var result = copy(values);
    result.reqMap = 'declined';
    if (!request) {
      return result;
    }
    var derived = {};
    if (request.derivedContestantType === 'TEAM') {
      derived.min_players_in_team = request.teamSize;
      derived.max_players_in_team = request.teamSize;
      derived.max_teams = request.participantLimit;
    } else if (request.derivedContestantType === 'SOLO') {
      derived.max_players = request.participantLimit;
    }
    Object.keys(derived).forEach(function (key) {
      if (
        isSet(derived[key]) &&
        String(values[key]).trim() === String(derived[key])
      ) {
        result[key] = '';
      }
    });
    return result;
  }

  return {
    STEP_FIELDS: STEP_FIELDS,
    format: format,
    validate: validate,
    applicableScopes: applicableScopes,
    modeArea: modeArea,
    skipScoring: skipScoring,
    nextStep: nextStep,
    prevStep: prevStep,
    stepStatus: stepStatus,
    summaryLabel: summaryLabel,
    stepSummary: stepSummary,
    stepSubtitle: stepSubtitle,
    dependentChange: dependentChange,
    reviewBanner: reviewBanner,
    startParts: startParts,
    initialReqMap: initialReqMap,
    applyRequestValues: applyRequestValues,
    declineRequestValues: declineRequestValues
  };
});
