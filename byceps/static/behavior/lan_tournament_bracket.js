/**
 * LAN Tournament – Client-side bracket renderer.
 *
 * Adapted from Turniercss/turnier-shared.js rendering engine to consume
 * BYCEPS pre-computed match data.  Handles layout, DOM rendering, SVG
 * connectors, source badges, hover highlight and pan.
 *
 * Plain ES5 vanilla JS – no modules, no build step.
 * Structural CSS classes use the `lt-` prefix; modifier/state classes
 * are unprefixed (contract with the stylesheet).
 */

/* ===================================================================
 *  i18n helpers – translations embedded in bracket JSON by the server
 * =================================================================== */

/** Module-scoped translation strings, populated from data.strings. */
var _ltStrings = {};

/**
 * Look up a translated string by key.
 * Falls back to the provided fallback (or the key itself) if not found,
 * so the bracket renders in English even without translations.
 */
function _t(key, fallback) {
  if (_ltStrings && _ltStrings[key] != null) return _ltStrings[key];
  return (fallback != null) ? fallback : key;
}

/**
 * Simple singular/plural helper.
 * German uses the same 1/many split as English for count-based plurals.
 */
function _tp(count, singularKey, pluralKey) {
  return count === 1 ? _t(singularKey) : _t(pluralKey);
}

/* ===================================================================
 *  Utility helpers
 * =================================================================== */

function _ltEscapeHtml(value) {
  return String(value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}

function _ltNormalizePlayer(value) {
  return String(value || '')
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/gi, '-')
    .replace(/^-+|-+$/g, '');
}

function _ltClamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

function _ltPackPlayerData(keys) {
  var seen = {};
  var list = [];
  var i, k;
  if (!keys) return '||';
  for (i = 0; i < keys.length; i++) {
    k = keys[i];
    if (k && !seen[k]) {
      seen[k] = true;
      list.push(k);
    }
  }
  return '|' + list.join('|') + '|';
}

function _ltOverlapPlayers(a, b) {
  var bSet = {};
  var result = [];
  var i;
  if (!a || !b) return [];
  for (i = 0; i < b.length; i++) { bSet[b[i]] = true; }
  for (i = 0; i < a.length; i++) {
    if (a[i] && bSet[a[i]]) result.push(a[i]);
  }
  return result;
}

/* ===================================================================
 *  Match-ref generation & display formatting
 * =================================================================== */

/**
 * Generate a match_ref string from bracket, round, and match_order.
 *
 * Python schema: round is 0-based, match_order is 0-based.
 * match_ref convention: "WB R1 M1" (1-based round and match numbers).
 * For SE (bracket=null): "R1 M1".
 * For GF: "GF M1".
 *
 * @param {string|null} bracket  "WB", "LB", "GF", or null
 * @param {number}      round    0-based round number from Python
 * @param {number}      matchOrder  0-based match_order from Python
 * @returns {string}
 */
function _ltGenerateMatchRef(bracket, round, matchOrder) {
  var r = (round || 0) + 1;
  var m = (matchOrder || 0) + 1;
  if (bracket === 'GF') return 'GF M' + m;
  if (bracket === 'P3') return 'P3 M' + m;
  if (bracket === 'WB') return 'WB R' + r + ' M' + m;
  if (bracket === 'LB') return 'LB R' + r + ' M' + m;
  // SE or null bracket
  return 'R' + r + ' M' + m;
}

function _ltParseMatchRef(ref) {
  var value = String(ref || '').trim();
  var wbLb = value.match(/^(WB|LB)\s+R(\d+)\s+M(\d+)$/i);
  if (wbLb) {
    return { bracket: wbLb[1].toUpperCase(), round: Number(wbLb[2]), match: Number(wbLb[3]) };
  }
  var gf = value.match(/^GF\s+M(\d+)$/i);
  if (gf) {
    return { bracket: 'GF', round: 1, match: Number(gf[1]) };
  }
  var p3 = value.match(/^P3\s+M(\d+)$/i);
  if (p3) {
    return { bracket: 'P3', round: 1, match: Number(p3[1]) };
  }
  var se = value.match(/^R(\d+)\s+M(\d+)$/i);
  if (se) {
    return { bracket: null, round: Number(se[1]), match: Number(se[2]) };
  }
  return null;
}

function _ltFormatMatchRefDisplay(ref) {
  var parsed = _ltParseMatchRef(ref);
  if (!parsed) return String(ref || '').trim();
  if (parsed.bracket === 'GF') return _t('grandFinalGame', 'Grand Final \u2013 Game') + ' ' + parsed.match;
  if (parsed.bracket === 'P3') return _t('thirdPlaceGame', '3rd Place \u2013 Game') + ' ' + parsed.match;
  if (parsed.bracket === 'WB' || parsed.bracket === 'LB') {
    return parsed.bracket + ' ' + _t('rAbbrev', 'R') + parsed.round + ' \u2013 ' + _t('game', 'Game') + ' ' + parsed.match;
  }
  return _t('rAbbrev', 'R') + parsed.round + ' \u2013 ' + _t('game', 'Game') + ' ' + parsed.match;
}

function _ltFormatSourceRefDisplay(ref) {
  var parsed = _ltParseMatchRef(ref);
  if (!parsed) return String(ref || '').trim();
  if (parsed.bracket === 'GF') return _t('gfG', 'GF G') + parsed.match;
  if (parsed.bracket === 'P3') return _t('thirdM', '3rd M') + parsed.match;
  if (parsed.bracket === 'WB' || parsed.bracket === 'LB') {
    return parsed.bracket + ' ' + _t('rAbbrev', 'R') + parsed.round + ' ' + _t('mAbbrev', 'M') + parsed.match;
  }
  return _t('rAbbrev', 'R') + parsed.round + ' ' + _t('mAbbrev', 'M') + parsed.match;
}

function _ltGetSourceLabel(ref, sourceOutcome, currentBracket) {
  var compact = _ltFormatSourceRefDisplay(ref);
  var sourceBracket = ref ? String(ref).split(' ')[0] : '';
  var outcomeLabel = sourceOutcome === 'loser' ? _t('loser', 'Loser') : _t('winner', 'Winner');

  if (currentBracket === 'WB' && sourceBracket === 'LB' && sourceOutcome === 'winner') {
    return _t('lbWinner', 'LB Winner') + ' ' + compact;
  }
  return outcomeLabel + ' ' + _t('of', 'of') + ' ' + compact;
}

/* ===================================================================
 *  Data parsing layer  (server JSON -> internal model)
 * =================================================================== */

/**
 * Build an entrant view-model from a Python contestant object.
 *
 * Python contestant: { name, score, team_id, participant_id }
 * A contestant with name "TBD" or missing real IDs is a placeholder.
 *
 * @param {Object|null} contestant  Python contestant (or null/undefined).
 * @param {string}      placeholder Label when not yet determined.
 * @returns {Object}  { type, name, label, key, score, id }
 */
function _ltBuildEntrant(contestant, placeholder) {
  if (!contestant) {
    return { type: 'placeholder', name: placeholder || _t('tbd', 'TBD'), label: placeholder || _t('tbd', 'TBD'), key: '', score: null, id: null };
  }
  var hasRealId = !!(contestant.team_id || contestant.participant_id);
  var name = contestant.name || 'TBD';
  var isTBD = name === 'TBD';

  if (!hasRealId || isTBD) {
    return { type: 'placeholder', name: placeholder || _t('tbd', 'TBD'), label: placeholder || _t('tbd', 'TBD'), key: '', score: null, id: null };
  }

  var display = name;
  return {
    type: 'player',
    name: display,
    label: display,
    key: _ltNormalizePlayer(display),
    score: (contestant.score != null) ? contestant.score : null,
    id: contestant.team_id || contestant.participant_id,
    teamId: contestant.team_id ? String(contestant.team_id) : null,
    participantId: contestant.participant_id ? String(contestant.participant_id) : null
  };
}

/**
 * Format an entrant for rendering (view model).
 */
function _ltFormatEntrantForView(entrant) {
  if (!entrant) return { text: _t('tbd', 'TBD'), className: 'placeholder', key: '' };
  if (entrant.type === 'placeholder') return { text: entrant.label, className: 'placeholder', key: '' };
  if (entrant.type === 'defwin') return { text: '\u2014', className: 'defwin', key: '' };
  return {
    text: entrant.label || entrant.name || _t('tbd', 'TBD'),
    className: '',
    key: _ltNormalizePlayer(entrant.name)
  };
}

/**
 * Determine match status from the Python match data.
 *
 * @param {boolean} confirmed  Whether match is confirmed
 * @param {Object|null} topEntrant  Built top entrant
 * @param {Object|null} botEntrant  Built bottom entrant
 * @returns {Object}  { key: string, label: string }
 */
function _ltComputeStatus(confirmed, topEntrant, botEntrant) {
  var topReal = topEntrant && topEntrant.type === 'player';
  var botReal = botEntrant && botEntrant.type === 'player';

  if (confirmed) {
    return { key: 'done', label: _t('statusDone', 'Done') };
  }
  // One real contestant, other is defwin or absent -> auto-advance (DEFWIN)
  if ((topReal && !botReal) || (botReal && !topReal)) {
    return { key: 'auto', label: _t('statusDefwin', 'DEFWIN') };
  }
  if (topReal && botReal) {
    return { key: 'open', label: _t('statusOpen', 'Open') };
  }
  return { key: 'pending', label: _t('statusPending', 'Pending') };
}

/**
 * Detect the winner of a match.
 *
 * @param {boolean} confirmed
 * @param {Object} topEntrant  Built entrant
 * @param {Object} botEntrant  Built entrant
 * @param {number|null} topScore
 * @param {number|null} botScore
 * @returns {number|null}  1 = top wins, 2 = bottom wins, null = not decided
 */
function _ltDetectWinner(confirmed, topEntrant, botEntrant, topScore, botScore) {
  var topReal = topEntrant && topEntrant.type === 'player';
  var botReal = botEntrant && botEntrant.type === 'player';
  var topDefwin = topEntrant && topEntrant.type === 'defwin';
  var botDefwin = botEntrant && botEntrant.type === 'defwin';

  // Both defwin or both missing — no winner
  if (topDefwin && botDefwin) return null;

  // One side is defwin, other is real — real side auto-wins
  if (topDefwin && botReal) return 2;
  if (botDefwin && topReal) return 1;

  // Only one real contestant present — auto-advance
  if (topReal && !botReal && !botDefwin) return 1;
  if (botReal && !topReal && !topDefwin) return 2;

  // Both real, confirmed with scores
  if (confirmed && topReal && botReal) {
    var ts = (topScore != null) ? Number(topScore) : null;
    var bs = (botScore != null) ? Number(botScore) : null;
    if (ts !== null && bs !== null) {
      if (ts > bs) return 1;
      if (bs > ts) return 2;
    }
    // Equal scores or missing: no clear winner
    return null;
  }

  return null;
}

/**
 * Parse the server JSON into the renderer's internal data model.
 *
 * Consumes the Python `serialize_bracket_json()` output which has:
 *   { tournament, matches[], match_urls{uuid->url}, hover_data }
 *
 * Each match: { id, round, match_order, bracket, next_match_id,
 *               loser_next_match_id, confirmed, contestants[] }
 *
 * Groups matches by bracket side (WB/LB/GF) and round number,
 * computes statuses, builds the matchMap, and returns structured
 * round arrays ready for layout.
 *
 * @param {Object} json  The server-provided bracket data.
 * @returns {Object}  { tournament, winnerRounds, loserRounds, finalMatches, matchMap, matchUrls }
 */
function parseBracketData(json) {
  var tournament = json.tournament || {};
  var rawMatches = json.matches || [];
  var rawMatchUrls = json.match_urls || {};
  var hoverData = json.hover_data || { seats: {}, team_members: {} };
  var matchMap = {};
  var matchById = {};
  var winnerBuckets = {};  // roundNo -> [match, ...]
  var loserBuckets = {};
  var finalMatches = [];
  var thirdPlaceMatches = [];
  var matchUrls = {};
  var i, m, matchRef, parsed, bucket, roundKey;
  var topContestant, botContestant, entrantTop, entrantBot;
  var topScore, botScore, winnerIndex, status;
  var topPlayers, botPlayers, playerKeys;

  // First pass: build all matches and index by ID and ref
  for (i = 0; i < rawMatches.length; i++) {
    m = rawMatches[i];

    // Generate match_ref from Python fields
    matchRef = _ltGenerateMatchRef(m.bracket, m.round, m.match_order);
    parsed = _ltParseMatchRef(matchRef);
    if (!parsed) continue;

    // Map contestants: index 0 = top, index 1 = bottom
    topContestant = (m.contestants && m.contestants.length > 0) ? m.contestants[0] : null;
    botContestant = (m.contestants && m.contestants.length > 1) ? m.contestants[1] : null;

    entrantTop = _ltBuildEntrant(topContestant, _t('tbd', 'TBD'));
    entrantBot = _ltBuildEntrant(botContestant, _t('tbd', 'TBD'));

    topScore = (topContestant && topContestant.score != null) ? topContestant.score : null;
    botScore = (botContestant && botContestant.score != null) ? botContestant.score : null;

    // Defer defwin/auto-advance classification to pass 3 (after source refs
    // are wired) so that matches waiting for incomplete feeders are not
    // prematurely marked as DEFWIN.  Pass 1 only builds the raw entrants.
    winnerIndex = null;
    status = { key: 'pending', label: _t('statusPending', 'Pending') };

    topPlayers = (entrantTop.type === 'player') ? [entrantTop.key] : [];
    botPlayers = (entrantBot.type === 'player') ? [entrantBot.key] : [];
    playerKeys = topPlayers.concat(botPlayers);

    var internalMatch = {
      id: m.id,
      ref: matchRef,
      bracket: parsed.bracket || (m.bracket || null),
      roundNo: parsed.round || ((m.round || 0) + 1),
      matchNo: parsed.match || ((m.match_order || 0) + 1),
      confirmed: !!m.confirmed,
      topResolved: {
        entrant: entrantTop,
        sourceRef: null,
        sourceOutcome: null
      },
      bottomResolved: {
        entrant: entrantBot,
        sourceRef: null,
        sourceOutcome: null
      },
      scores: [topScore, botScore],
      winnerIndex: winnerIndex,
      winnerEntrant: winnerIndex === 1 ? entrantTop : (winnerIndex === 2 ? entrantBot : null),
      loserEntrant: winnerIndex === 1 ? entrantBot : (winnerIndex === 2 ? entrantTop : null),
      isComplete: status.key === 'done' || status.key === 'auto',
      isAutoAdvanced: status.key === 'auto',
      isReadyToPlay: status.key === 'open',
      isWaitingForPlayers: status.key === 'pending',
      playerKeys: playerKeys,
      nextMatchId: m.next_match_id || null,
      loserNextMatchId: m.loser_next_match_id || null,
      isDead: (m.incoming_feed_count === 0) &&
              ((m.contestants || []).length === 0) &&
              !m.next_match_id
    };

    // Dead matches are structural DEFWINs — classify them accordingly
    // so they render with DEFWIN styling, not "Pending".
    if (internalMatch.isDead) {
      internalMatch.isComplete = true;
      internalMatch.isAutoAdvanced = true;
      internalMatch.isWaitingForPlayers = false;
    }

    // Index by both ref and UUID for cross-referencing
    matchMap[matchRef] = internalMatch;
    matchById[m.id] = internalMatch;

    // Map match_urls from UUID-keyed to ref-keyed
    if (rawMatchUrls[m.id] != null) {
      matchUrls[matchRef] = rawMatchUrls[m.id];
    }
    // Also keep UUID-keyed URLs for fallback
    matchUrls[m.id] = rawMatchUrls[m.id] || null;

    if (parsed.bracket === 'WB') {
      roundKey = parsed.round;
      if (!winnerBuckets[roundKey]) winnerBuckets[roundKey] = [];
      winnerBuckets[roundKey].push(internalMatch);
    } else if (parsed.bracket === 'LB') {
      roundKey = parsed.round;
      if (!loserBuckets[roundKey]) loserBuckets[roundKey] = [];
      loserBuckets[roundKey].push(internalMatch);
    } else if (parsed.bracket === 'GF') {
      finalMatches.push(internalMatch);
    } else if (parsed.bracket === 'P3') {
      thirdPlaceMatches.push(internalMatch);
    } else {
      // SE (null bracket) — treat as winner bracket
      roundKey = parsed.round;
      if (!winnerBuckets[roundKey]) winnerBuckets[roundKey] = [];
      winnerBuckets[roundKey].push(internalMatch);
    }
  }

  // Second pass: compute source refs from next_match_id / loser_next_match_id
  // For each match that has next_match_id, the target match gets a source ref
  for (i = 0; i < rawMatches.length; i++) {
    m = rawMatches[i];
    var sourceMatch = matchById[m.id];
    if (!sourceMatch) continue;

    // Winner goes to next_match_id
    if (m.next_match_id && matchById[m.next_match_id]) {
      var targetMatch = matchById[m.next_match_id];
      _ltAssignSourceRef(targetMatch, sourceMatch.ref, 'winner');
    }

    // Loser goes to loser_next_match_id
    if (m.loser_next_match_id && matchById[m.loser_next_match_id]) {
      var loserTarget = matchById[m.loser_next_match_id];
      _ltAssignSourceRef(loserTarget, sourceMatch.ref, 'loser');
    }
  }

  // Third pass: classify with full source-ref knowledge.
  // Now that pass 2 has wired all sourceRefs, we can accurately decide
  // whether an empty slot is a true structural DEFWIN (no feeder) or a
  // pending slot (feeder match exists but hasn't completed yet).
  // We check the feeder's `isDead` flag (structural, set in pass 1)
  // rather than `isComplete` (dynamic) to avoid iteration-order bugs.
  for (i = 0; i < rawMatches.length; i++) {
    m = rawMatches[i];
    var classify = matchById[m.id];
    if (!classify || classify.isDead) continue;

    var topEnt = classify.topResolved.entrant;
    var botEnt = classify.bottomResolved.entrant;
    var topSrc = classify.topResolved.sourceRef;
    var botSrc = classify.bottomResolved.sourceRef;

    // Convert placeholder to defwin ONLY if no pending feeder
    if (topEnt.type === 'placeholder' && m.contestants && m.contestants.length <= 1) {
      var topFeederPending = topSrc && matchMap[topSrc] && !matchMap[topSrc].isDead;
      if (!topFeederPending) {
        topEnt = { type: 'defwin', name: '', label: '', key: '', score: null, id: null };
        classify.topResolved.entrant = topEnt;
      }
    }
    if (botEnt.type === 'placeholder' && m.contestants && m.contestants.length <= 1) {
      var botFeederPending = botSrc && matchMap[botSrc] && !matchMap[botSrc].isDead;
      if (!botFeederPending) {
        botEnt = { type: 'defwin', name: '', label: '', key: '', score: null, id: null };
        classify.bottomResolved.entrant = botEnt;
      }
    }

    // If any slot is still a placeholder after the defwin checks above,
    // the match is waiting for a feeder — force "Pending" status.
    // _ltComputeStatus and _ltDetectWinner only distinguish 'player' and
    // 'defwin'; a surviving 'placeholder' would be misclassified as DEFWIN.
    if (topEnt.type === 'placeholder' || botEnt.type === 'placeholder') {
      classify.winnerIndex = null;
      classify.isComplete = false;
      classify.isAutoAdvanced = false;
      classify.isReadyToPlay = false;
      classify.isWaitingForPlayers = true;
      classify.winnerEntrant = null;
      classify.loserEntrant = null;
      continue;
    }

    // Now classify with accurate entrant types
    var ts = classify.scores[0];
    var bs = classify.scores[1];
    classify.winnerIndex = _ltDetectWinner(classify.confirmed, topEnt, botEnt, ts, bs);
    var st = _ltComputeStatus(classify.confirmed, topEnt, botEnt);
    classify.isComplete = st.key === 'done' || st.key === 'auto';
    classify.isAutoAdvanced = st.key === 'auto';
    classify.isReadyToPlay = st.key === 'open';
    classify.isWaitingForPlayers = st.key === 'pending';
    classify.winnerEntrant = classify.winnerIndex === 1 ? topEnt : (classify.winnerIndex === 2 ? botEnt : null);
    classify.loserEntrant = classify.winnerIndex === 1 ? botEnt : (classify.winnerIndex === 2 ? topEnt : null);
  }

  var winnerRounds = _ltBucketsToRounds(winnerBuckets, 'WB');
  var loserRounds = _ltBucketsToRounds(loserBuckets, 'LB');

  // Separate GF matches from other finals so ALL GF matches can be
  // integrated into winnerRounds (GF M1 as "Grand Final", GF M2+ as
  // "Bracket Reset" columns to the right).
  var gfMatches = [];
  var extraFinals = [];
  for (i = 0; i < finalMatches.length; i++) {
    if (finalMatches[i].bracket === 'GF') {
      gfMatches.push(finalMatches[i]);
    } else {
      extraFinals.push(finalMatches[i]);
    }
  }
  gfMatches.sort(function(a, b) { return a.matchNo - b.matchNo; });

  if (gfMatches.length > 0 && winnerRounds.length > 0) {
    // GF M1 — main Grand Final
    winnerRounds.push({
      bracket: 'GF',
      displayBracket: 'WB',
      roundNo: winnerRounds.length + 1,
      matches: [gfMatches[0]],
      title: _t('grandFinal', 'Grand Final'),
      subtitle: _t('wbWinnerVsLbWinner', 'WB Winner vs LB Winner')
    });
    // GF M2+ — bracket reset (rendered as next column to the right).
    // displayBracket stays 'GF' so that the source-label system treats
    // GF M1 → GF M2 as same-bracket (no misleading "Winner of" badge).
    for (i = 1; i < gfMatches.length; i++) {
      winnerRounds.push({
        bracket: 'GF',
        displayBracket: 'GF',
        roundNo: winnerRounds.length + 1,
        matches: [gfMatches[i]],
        title: _t('bracketReset', 'Bracket Reset'),
        subtitle: _t('grandFinalDecisive', 'Grand Final \u2013 Decisive Game')
      });
    }
  } else {
    // Fallback: no WB context, treat GF as extra finals
    for (i = 0; i < gfMatches.length; i++) {
      extraFinals.push(gfMatches[i]);
    }
  }

  // Decorate round titles
  winnerRounds = _ltDecorateRounds(winnerRounds);
  loserRounds = _ltDecorateRounds(loserRounds);

  return {
    tournament: tournament,
    winnerRounds: winnerRounds,
    loserRounds: loserRounds,
    finalMatches: extraFinals,
    thirdPlaceMatches: thirdPlaceMatches,
    matchMap: matchMap,
    matchUrls: matchUrls,
    hoverData: hoverData
  };
}

/**
 * Compute placement ranks (1st, 2nd, 3rd) for completed bracket matches.
 *
 * Mutates each match object in parsed data to add:
 *   match.topPlacement  (1, 2, 3, or null)
 *   match.bottomPlacement (1, 2, 3, or null)
 *
 * SE logic:
 *   - Last round of winnerRounds = final: winner → 1, loser → 2
 *   - Second-to-last round = semifinal: losers → 3
 *   - Third-place match (P3): winner → 3 (overrides SF losers)
 *
 * DE logic:
 *   - GF match (last round of winnerRounds with bracket === 'GF') = final: winner → 1, loser → 2
 *   - If GF M2 (bracket reset) exists: GF M1 placements cleared; GF M2 decides rank 1/2
 *   - If GF M2 exists but incomplete: no placements on either GF match
 *   - Third-place match (P3): winner → 3
 *   - If no P3 match, LB final loser → 3
 *
 * @param {Object} parsed  The output of parseBracketData().
 */
function _ltComputePlacements(parsed) {
  var winnerRounds = parsed.winnerRounds;
  var loserRounds = parsed.loserRounds;
  var thirdPlaceMatches = parsed.thirdPlaceMatches;
  var isDE = loserRounds.length > 0;
  var i, match;

  // Find the final round (last round in winnerRounds)
  var finalRound = winnerRounds.length > 0 ? winnerRounds[winnerRounds.length - 1] : null;

  // Tag the final match (rank 1 for winner, rank 2 for loser)
  if (finalRound) {
    for (i = 0; i < finalRound.matches.length; i++) {
      match = finalRound.matches[i];
      if (!match.isComplete) continue;
      match.trophyMatch = true;
      if (match.winnerIndex === 1) {
        match.topPlacement = 1;
        match.bottomPlacement = 2;
      } else if (match.winnerIndex === 2) {
        match.topPlacement = 2;
        match.bottomPlacement = 1;
      }
    }
  }

  // DE bracket-reset: if GF M2 exists in finalMatches, it is the decisive match.
  // GF M1 placements must be cleared regardless (the bracket is not yet decided,
  // or the true result is on GF M2).
  if (isDE && finalRound) {
    var gfResetMatch = null;
    for (i = 0; i < parsed.finalMatches.length; i++) {
      if (_ltIsGFResetMatch(parsed.finalMatches[i])) {
        gfResetMatch = parsed.finalMatches[i];
        break;
      }
    }
    if (gfResetMatch) {
      // Clear premature GF M1 placements
      for (i = 0; i < finalRound.matches.length; i++) {
        match = finalRound.matches[i];
        delete match.topPlacement;
        delete match.bottomPlacement;
      }
      // If GF M2 is complete, it determines rank 1 and rank 2
      if (gfResetMatch.isComplete) {
        gfResetMatch.trophyMatch = true;
        if (gfResetMatch.winnerIndex === 1) {
          gfResetMatch.topPlacement = 1;
          gfResetMatch.bottomPlacement = 2;
        } else if (gfResetMatch.winnerIndex === 2) {
          gfResetMatch.topPlacement = 2;
          gfResetMatch.bottomPlacement = 1;
        }
      }
    }
  }

  // Tag semifinal losers (rank 3) — second-to-last round
  if (!isDE) {
    // SE: second-to-last winnerRound = semifinal
    var sfRound = winnerRounds.length >= 2 ? winnerRounds[winnerRounds.length - 2] : null;
    if (sfRound) {
      for (i = 0; i < sfRound.matches.length; i++) {
        match = sfRound.matches[i];
        if (!match.isComplete) continue;
        if (match.winnerIndex === 1) {
          match.bottomPlacement = 3;
        } else if (match.winnerIndex === 2) {
          match.topPlacement = 3;
        }
      }
    }
  } else {
    // DE: if no third-place match, the LB final loser gets rank 3
    if (thirdPlaceMatches.length === 0 && loserRounds.length > 0) {
      var lbFinalRound = loserRounds[loserRounds.length - 1];
      for (i = 0; i < lbFinalRound.matches.length; i++) {
        match = lbFinalRound.matches[i];
        if (!match.isComplete) continue;
        match.trophyMatch = true;
        if (match.winnerIndex === 1) {
          match.bottomPlacement = 3;
        } else if (match.winnerIndex === 2) {
          match.topPlacement = 3;
        }
      }
    }
  }

  // Tag third-place match (P3): winner → rank 3
  for (i = 0; i < thirdPlaceMatches.length; i++) {
    match = thirdPlaceMatches[i];
    if (!match.isComplete) continue;
    match.trophyMatch = true;
    if (match.winnerIndex === 1) {
      match.topPlacement = 3;
    } else if (match.winnerIndex === 2) {
      match.bottomPlacement = 3;
    }
  }
}

/**
 * Assign a source ref to the first empty slot (top or bottom) of a target match.
 */
function _ltAssignSourceRef(targetMatch, sourceRef, outcome) {
  if (!targetMatch.topResolved.sourceRef) {
    targetMatch.topResolved.sourceRef = sourceRef;
    targetMatch.topResolved.sourceOutcome = outcome;
  } else if (!targetMatch.bottomResolved.sourceRef) {
    targetMatch.bottomResolved.sourceRef = sourceRef;
    targetMatch.bottomResolved.sourceOutcome = outcome;
  }
  // If both slots already assigned, drop it (shouldn't happen in valid brackets)
}

/**
 * Convert a bucket map { roundNo: [match,...] } into a sorted array of round
 * objects, each with a `matches` array sorted by matchNo.
 */
function _ltBucketsToRounds(buckets, bracket) {
  var keys = [];
  var k;
  for (k in buckets) {
    if (buckets.hasOwnProperty(k)) keys.push(Number(k));
  }
  keys.sort(function(a, b) { return a - b; });

  var rounds = [];
  var i;
  for (i = 0; i < keys.length; i++) {
    var matches = buckets[keys[i]].slice();
    matches.sort(function(a, b) { return a.matchNo - b.matchNo; });
    rounds.push({
      bracket: bracket,
      roundNo: keys[i],
      matches: matches
    });
  }
  return rounds;
}

/**
 * Add title/subtitle to each round.
 */
function _ltDecorateRounds(rounds) {
  var i, round, matchCount, title, subtitle;
  for (i = 0; i < rounds.length; i++) {
    round = rounds[i];
    if (round.title) continue;  // already decorated (e.g. GF)
    matchCount = round.matches.length;
    title = _t('round', 'Round') + ' ' + (i + 1);
    subtitle = matchCount + ' ' + _tp(matchCount, 'matchSingular', 'matchPlural');
    round.title = title;
    round.subtitle = subtitle;
  }
  return rounds;
}


/* ===================================================================
 *  Layout engine
 * =================================================================== */

/**
 * Base dimension constants, responsive to viewport width.
 */
function getBaseDims() {
  var mobile = window.innerWidth <= 700;
  return {
    mobile: mobile,
    titleHeight: mobile ? 44 : 46,
    titleGap: mobile ? 8 : 5,
    slotHeight: mobile ? 74 : 68,
    matchWidth: mobile ? 246 : 300,
    connectorGap: mobile ? 64 : 76,
    leftInset: mobile ? 12 : 102,
    padding: mobile ? 7 : 7,
    labelHeight: mobile ? 26 : 30,
    teamHeight: mobile ? 30 : 36,
    rowGap: mobile ? 5 : 5,
    sourceBadgeMinWidth: mobile ? 108 : 130,
    sourceBadgeHeight: 24,
    fontScale: 1
  };
}

/**
 * Estimate the pixel width a source badge label will occupy.
 */
function _ltEstimateSourceLabelWidth(text, statusText, dims) {
  var labelWidth = (text || '').length * (dims.mobile ? 6.2 : 6.6);
  var statusWidth = statusText ? 14 : 0;
  var base = Math.max(dims.sourceBadgeMinWidth, labelWidth + statusWidth + 24);
  var upper = dims.mobile ? 220 : 270;
  return Math.max(94, Math.min(upper, base));
}

/**
 * Decide whether to show an external source badge for this reference.
 */
function shouldShowExternalSource(sourceRef, currentBracket) {
  var sourceBracket = sourceRef ? String(sourceRef).split(' ')[0] : '';
  return !!(sourceRef && (currentBracket === 'P3' || sourceBracket !== currentBracket));
}

/**
 * Get match status metadata (key + label) for a given match ref.
 */
function _ltGetMatchStatusFromMap(ref, matchMap) {
  var m = matchMap[ref];
  if (!m) return { key: 'pending', label: _t('statusPending', 'Pending') };
  if (m.isAutoAdvanced) return { key: 'auto', label: _t('statusDefwin', 'DEFWIN') };
  if (m.isComplete) return { key: 'done', label: _t('statusDone', 'Done') };
  if (m.isReadyToPlay) return { key: 'open', label: _t('statusOpen', 'Open') };
  return { key: 'pending', label: _t('statusPending', 'Pending') };
}

/**
 * Get the status label text for a match ref.
 */
function _ltGetMatchStatusText(ref, matchMap) {
  return _ltGetMatchStatusFromMap(ref, matchMap).label;
}

/**
 * Get the status metadata directly from a match object.
 */
function _ltGetMatchStatusMetaFromMatch(match) {
  if (!match) return { key: 'pending', label: _t('statusPending', 'Pending') };
  if (match.isAutoAdvanced) return { key: 'auto', label: _t('statusDefwin', 'DEFWIN') };
  if (match.isComplete) return { key: 'done', label: _t('statusDone', 'Done') };
  if (match.isReadyToPlay) return { key: 'open', label: _t('statusOpen', 'Open') };
  return { key: 'pending', label: _t('statusPending', 'Pending') };
}

/**
 * Scan rounds for the maximum width a source badge will need.
 */
function _ltGetMaxSourceBadgeWidth(rounds, dims, matchMap, settings) {
  if (!rounds || !rounds.length || !matchMap) return 0;

  var maxWidth = 0;
  var i, j, round, match, slots, s, ref, outcome, currentBracket, labelText, statusText, w;

  for (i = 0; i < rounds.length; i++) {
    round = rounds[i];
    currentBracket = round.displayBracket || round.bracket;

    for (j = 0; j < round.matches.length; j++) {
      match = round.matches[j];
      slots = [
        { ref: match.topResolved ? match.topResolved.sourceRef : null, outcome: match.topResolved ? match.topResolved.sourceOutcome : null },
        { ref: match.bottomResolved ? match.bottomResolved.sourceRef : null, outcome: match.bottomResolved ? match.bottomResolved.sourceOutcome : null }
      ];
      for (s = 0; s < slots.length; s++) {
        ref = slots[s].ref;
        outcome = slots[s].outcome;
        if (!shouldShowExternalSource(ref, currentBracket)) continue;
        labelText = _ltGetSourceLabel(ref, outcome, currentBracket);
        statusText = settings.showSourceStatusInBadge ? _ltGetMatchStatusText(ref, matchMap) : '';
        w = _ltEstimateSourceLabelWidth(labelText, statusText, dims);
        if (w > maxWidth) maxWidth = w;
      }
    }
  }

  return maxWidth;
}

/**
 * Compute fitted dimensions that scale columns to the available width.
 */
function getFittedDims(columnCount, rounds, matchMap, settings, appEl, options) {
  var opts = options || {};
  var base = getBaseDims();
  var availableWidth = Math.max(320, Math.min(window.innerWidth - (base.mobile ? 22 : 44), appEl.clientWidth || window.innerWidth));
  var baseSourceBadgeWidth = opts.skipExternalSources ? 0 : _ltGetMaxSourceBadgeWidth(rounds, base, matchMap, settings);
  if (opts.minSourceBadgeWidth > baseSourceBadgeWidth) baseSourceBadgeWidth = opts.minSourceBadgeWidth;
  var hasExternalSources = baseSourceBadgeWidth > 0;
  var gapBase = hasExternalSources ? Math.max(base.connectorGap, baseSourceBadgeWidth + 12) : base.connectorGap;
  var leftInsetBase = hasExternalSources ? Math.max(base.leftInset, gapBase - 6) : base.leftInset;
  var baseWidth = (leftInsetBase * 2) + (columnCount * base.matchWidth) + (Math.max(0, columnCount - 1) * gapBase);
  var fit = Math.min(1, availableWidth / baseWidth);
  var compactFit = Math.max(base.mobile ? 0.9 : 0.78, fit);

  var matchWidth = _ltClamp(Math.round(base.matchWidth * compactFit), base.mobile ? 220 : 210, base.matchWidth);
  var slotHeight = _ltClamp(Math.round(base.slotHeight * Math.max(0.84, compactFit)), base.mobile ? 68 : 58, base.slotHeight);
  var titleHeight = _ltClamp(Math.round(base.titleHeight * Math.max(0.88, compactFit)), base.mobile ? 40 : 40, base.titleHeight);
  var labelHeight = _ltClamp(Math.round(base.labelHeight * Math.max(0.9, compactFit)), base.mobile ? 24 : 26, base.labelHeight);
  var teamHeight = _ltClamp(Math.round(base.teamHeight * Math.max(0.9, compactFit)), base.mobile ? 28 : 32, base.teamHeight);
  var padding = _ltClamp(Math.round(base.padding * Math.max(0.8, compactFit)), 6, base.padding);
  var rowGap = _ltClamp(Math.round(base.rowGap * Math.max(0.84, compactFit)), 4, base.rowGap);
  var sourceBadgeMinWidth = _ltClamp(Math.round(base.sourceBadgeMinWidth * compactFit), 100, base.sourceBadgeMinWidth);
  var fontScale = _ltClamp(compactFit, base.mobile ? 0.94 : 0.86, 1);

  var provisionalDims = {
    mobile: base.mobile,
    matchWidth: matchWidth,
    slotHeight: slotHeight,
    titleHeight: titleHeight,
    titleGap: base.titleGap,
    labelHeight: labelHeight,
    teamHeight: teamHeight,
    padding: padding,
    rowGap: rowGap,
    sourceBadgeMinWidth: sourceBadgeMinWidth,
    sourceBadgeHeight: base.sourceBadgeHeight,
    fontScale: fontScale,
    connectorGap: base.connectorGap,
    leftInset: base.leftInset
  };

  var minGapNeeded = hasExternalSources ? Math.round(_ltGetMaxSourceBadgeWidth(rounds, provisionalDims, matchMap, settings) + 12) : 0;
  var connectorGap = hasExternalSources
    ? Math.max(Math.round(gapBase * compactFit), minGapNeeded)
    : _ltClamp(Math.round(base.connectorGap * compactFit), base.mobile ? 46 : 30, base.connectorGap);
  var leftInset = hasExternalSources
    ? Math.max(_ltClamp(Math.round(base.leftInset * compactFit), base.mobile ? 10 : 20, leftInsetBase), connectorGap - 6)
    : _ltClamp(Math.round(base.leftInset * compactFit), base.mobile ? 10 : 20, base.leftInset);

  return {
    mobile: base.mobile,
    titleHeight: titleHeight,
    titleGap: base.titleGap,
    slotHeight: slotHeight,
    matchWidth: matchWidth,
    connectorGap: connectorGap,
    leftInset: leftInset,
    padding: padding,
    labelHeight: labelHeight,
    teamHeight: teamHeight,
    rowGap: rowGap,
    sourceBadgeMinWidth: sourceBadgeMinWidth,
    sourceBadgeHeight: base.sourceBadgeHeight,
    fontScale: fontScale
  };
}

/**
 * Total height of a single match card.
 */
function getMatchHeight(dims) {
  var h = (dims.padding * 2) + dims.labelHeight + dims.rowGap + dims.teamHeight + dims.rowGap + dims.teamHeight;
  return h + (h % 2);  // ensure even so /2 always yields integer pixel coords
}

/**
 * Vertical centre offsets for top/bottom team rows.
 */
function getTeamOffsets(dims) {
  var top = dims.padding + dims.labelHeight + dims.rowGap + (dims.teamHeight / 2);
  var bottom = top + dims.teamHeight + dims.rowGap;
  return { top: top, bottom: bottom };
}

/**
 * Assign synthetic geometry for matches rendered in card layout
 * (not part of the round layout engine). These matches use CSS
 * flow positioning inside card wrappers, so absolute coordinates
 * are zeroed out.
 */
function _ltAssignCardGeom(match, dims) {
  var mh = getMatchHeight(dims);
  var offsets = getTeamOffsets(dims);
  match.geom = {
    centerY: mh / 2,
    boxLeft: 0,
    boxRight: dims.matchWidth,
    boxTop: 0,
    boxBottom: mh,
    teamTopY: offsets.top,
    teamBottomY: offsets.bottom
  };
}

/**
 * Assign absolute (x, y) geometry to every match in every round.
 *
 * @param {Array}  rounds    Array of round objects, each with a `matches` array.
 * @param {number} fieldSize Number of first-round slots (power of 2).
 * @param {Object} dims      Fitted dimensions from getFittedDims().
 * @returns {Object}  { width, height, bracketHeight, headerOffset, matchHeight }
 */
function layoutRounds(rounds, fieldSize, dims) {
  var matchHeight = getMatchHeight(dims);
  var teamOffsets = getTeamOffsets(dims);
  var bracketHeight = fieldSize * dims.slotHeight;
  var headerOffset = dims.titleHeight + dims.titleGap;
  var x = dims.leftInset;
  var i, j, round, step, centerY, boxTop;

  for (i = 0; i < rounds.length; i++) {
    round = rounds[i];
    round.columnX = x;
    round.columnWidth = dims.matchWidth;
    round.boxLeft = x;
    round.boxRight = x + dims.matchWidth;

    step = bracketHeight / round.matches.length;

    for (j = 0; j < round.matches.length; j++) {
      centerY = headerOffset + (step * (j + 0.5));
      boxTop = centerY - (matchHeight / 2);

      round.matches[j].geom = {
        centerY: centerY,
        boxLeft: round.boxLeft,
        boxRight: round.boxRight,
        boxTop: boxTop,
        boxBottom: boxTop + matchHeight,
        teamTopY: boxTop + teamOffsets.top,
        teamBottomY: boxTop + teamOffsets.bottom
      };
    }

    x += round.columnWidth;
    if (i < rounds.length - 1) x += dims.connectorGap;
  }

  var rightSafety = Math.max(26, dims.padding + 18);

  return {
    width: x + dims.leftInset + rightSafety,
    height: headerOffset + bracketHeight,
    bracketHeight: bracketHeight,
    headerOffset: headerOffset,
    matchHeight: matchHeight
  };
}


/* ===================================================================
 *  SVG connector lines
 * =================================================================== */

/**
 * Create an SVG <path> with stroke styling for bracket connectors.
 */
function _ltSvgPath(d, strong, className, players) {
  var path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  path.setAttribute('d', d);
  path.setAttribute('fill', 'none');
  path.setAttribute('stroke', strong ? 'var(--lt-line-strong)' : 'var(--lt-line)');
  path.setAttribute('stroke-width', strong ? '2.25' : '2');
  path.setAttribute('stroke-linecap', 'round');
  path.setAttribute('stroke-linejoin', 'round');
  path.setAttribute('class', className || 'lt-connector');
  path.setAttribute('data-players', _ltPackPlayerData(players));
  return path;
}

/**
 * Create the root SVG element for bracket lines.
 */
function _ltCreateSvg(width, height) {
  var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('class', 'lt-lines-layer');
  svg.setAttribute('width', width);
  svg.setAttribute('height', height);
  svg.setAttribute('viewBox', '0 0 ' + width + ' ' + height);
  return svg;
}

/**
 * Build a unique route ID for a source->target connection.
 */
function _ltBuildRouteId(sourceRef, targetRef, slotName) {
  return (sourceRef || '?') + '->' + (targetRef || '?') + ':' + slotName;
}

/**
 * Add a horizontal source connector line to the SVG.
 */
function _ltAddSourceConnector(svg, options) {
  var safeStart = Math.min(options.startX, options.endX - 8);
  var y = Math.round(options.y);
  var path = _ltSvgPath('M ' + safeStart + ' ' + y + ' H ' + options.endX, false, 'lt-source-connector', options.players || []);
  path.setAttribute('data-route', options.routeId);
  path.setAttribute('data-source-ref', options.sourceRef);
  path.setAttribute('data-target-ref', options.targetRef);
  svg.appendChild(path);
}

/**
 * Add a bracket connector between two match boxes.
 */
function addConnector(svg, fromMatch, toMatch, dims, strong) {
  var players = _ltOverlapPlayers(fromMatch.playerKeys, toMatch.playerKeys);
  var joinX = Math.round(fromMatch.geom.boxRight + (dims.connectorGap / 2));

  // When the target match has an external source (cross-bracket feed) on
  // one slot, route this internal connector to the OTHER slot so the two
  // entry lines are visually distinct instead of overlapping at centerY.
  var targetY = toMatch.geom.centerY;
  if (toMatch.topResolved && toMatch.bottomResolved && toMatch.bracket) {
    var topSrc = toMatch.topResolved.sourceRef;
    var botSrc = toMatch.bottomResolved.sourceRef;
    var topIsExt = topSrc ? shouldShowExternalSource(topSrc, toMatch.bracket) : false;
    var botIsExt = botSrc ? shouldShowExternalSource(botSrc, toMatch.bracket) : false;
    if (topIsExt && !botIsExt) {
      targetY = toMatch.geom.teamBottomY;
    } else if (botIsExt && !topIsExt) {
      targetY = toMatch.geom.teamTopY;
    }
  }

  // When targeting a specific slot (external source present), also start
  // the connector at that slot Y.  In 1:1 LB rounds both matches share
  // identical geometry, so this produces a clean straight horizontal line
  // instead of an L-shaped path that bends from centerY to the slot.
  var startY = (targetY !== toMatch.geom.centerY) ? targetY : fromMatch.geom.centerY;

  var d;
  if (startY === targetY) {
    d = 'M ' + fromMatch.geom.boxRight + ' ' + startY
      + ' H ' + toMatch.geom.boxLeft;
  } else {
    d = 'M ' + fromMatch.geom.boxRight + ' ' + startY
      + ' H ' + joinX
      + ' V ' + targetY
      + ' H ' + toMatch.geom.boxLeft;
  }
  svg.appendChild(_ltSvgPath(d, strong, 'lt-connector', players));
}

/**
 * Draw all connector lines between successive rounds.
 *
 * Standard elimination pattern: if round N has 2x the matches of round N+1,
 * connect pairs. If equal match counts, connect 1:1 (LB same-size rounds).
 */
function drawConnectorLines(svg, rounds, dims) {
  var i, current, next, j;
  for (i = 0; i < rounds.length - 1; i++) {
    current = rounds[i];
    next = rounds[i + 1];

    if (current.matches.length === next.matches.length * 2) {
      for (j = 0; j < next.matches.length; j++) {
        addConnector(svg, current.matches[j * 2], next.matches[j], dims, true);
        addConnector(svg, current.matches[(j * 2) + 1], next.matches[j], dims, true);
      }
    } else if (current.matches.length === next.matches.length) {
      for (j = 0; j < next.matches.length; j++) {
        addConnector(svg, current.matches[j], next.matches[j], dims, false);
      }
    }
  }
}


/* ===================================================================
 *  DOM rendering
 * =================================================================== */

/**
 * Create a round title header element.
 */
function createRoundTitle(round, dims) {
  var el = document.createElement('div');
  el.className = 'lt-round-title';
  el.style.left = round.columnX + 'px';
  el.style.top = '0px';
  el.style.width = round.columnWidth + 'px';
  el.style.height = dims.titleHeight + 'px';
  el.style.fontSize = (0.93 * dims.fontScale) + 'rem';
  el.innerHTML =
    '<span>' + _ltEscapeHtml(round.title) + '</span>' +
    '<small>' + _ltEscapeHtml(round.subtitle) + '</small>';
  return el;
}

/**
 * Build a single team row (top or bottom contestant) inside a match card.
 *
 * @param {Object}  entrant    Entrant data object.
 * @param {boolean} isWinner   Whether this entrant won the match.
 * @param {number|null} score  Entrant score.
 * @param {Object}  dims       Dimension/layout config.
 * @param {string}  matchRef   Match reference string.
 * @param {Object}  hoverData  Hover/tooltip data.
 * @param {number|null} placement  Rank placement (1, 2, 3) or null.
 * @param {boolean}    trophyMatch Whether this match is a trophy-worthy match (final/GF/3rd-place).
 */
function buildTeamRow(entrant, isWinner, score, dims, matchRef, hoverData, placement, trophyMatch) {
  var team = _ltFormatEntrantForView(entrant);
  var classes = ['lt-team'];
  if (team.className) classes.push(team.className);
  if (isWinner && team.className !== 'defwin') classes.push('winner');
  if (placement && team.className !== 'defwin') classes.push('lt-team-rank-' + placement);
  var scoreText = (score === 0 || score) ? String(score) : '-';
  var playerAttr = team.key ? ' data-player="' + _ltEscapeHtml(team.key) + '"' : '';
  var scoreAttrs = team.key
    ? ' data-player="' + _ltEscapeHtml(team.key) + '" data-hoverable="true" tabindex="0"'
    : matchRef
      ? ' data-match-ref="' + _ltEscapeHtml(matchRef) + '" data-hoverable="true" tabindex="0"'
      : '';

  // Build hover card tooltip if hover_data provides info for this entrant
  var hoverHtml = '';
  if (hoverData && entrant && entrant.type === 'player') {
    var teamId = entrant.teamId || null;
    var participantId = entrant.participantId || null;
    var hoverMembers = teamId ? (hoverData.team_members || {})[teamId] : null;
    var hoverSeat = participantId ? (hoverData.seats || {})[participantId] : null;

    if (hoverMembers && hoverMembers.length > 0) {
      hoverHtml = '<span class="lt-hover-card">';
      for (var h = 0; h < hoverMembers.length; h++) {
        var memberName = _ltEscapeHtml(hoverMembers[h][0]);
        var memberSeat = hoverMembers[h][1] ? ' [' + _ltEscapeHtml(hoverMembers[h][1]) + ']' : '';
        hoverHtml += '<span class="lt-hover-member">' + memberName + memberSeat + '</span>';
      }
      hoverHtml += '</span>';
    } else if (hoverSeat) {
      hoverHtml = '<span class="lt-hover-card"><span class="lt-hover-seat">' +
        _ltEscapeHtml(hoverSeat) + '</span></span>';
    }
  }

  // Trophy icon SVG for 1st / 2nd / 3rd place
  var trophyColors = { 1: '#FFD700', 2: '#C0C0C0', 3: '#CD7F32' };
  var trophyLabels = { 1: _t('winner', 'Winner'), 2: _t('secondPlace', '2nd Place'), 3: _t('thirdPlace', '3rd Place') };
  var trophyHtml = (trophyMatch && placement && trophyColors[placement])
    ? '<span class="lt-trophy-icon" aria-label="' + trophyLabels[placement] + '">' +
      '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="' + trophyColors[placement] + '" aria-hidden="true">' +
      '<path d="M19 5h-2V3H7v2H5c-1.1 0-2 .9-2 2v1c0 2.55 1.92 4.63 4.39 4.94A5.01 5.01 0 0 0 11 15.9V19H7v2h10v-2h-4v-3.1a5.01 5.01 0 0 0 3.61-2.96C19.08 12.63 21 10.55 21 8V7c0-1.1-.9-2-2-2zM5 8V7h2v3.82C5.84 10.4 5 9.3 5 8zm14 0c0 1.3-.84 2.4-2 2.82V7h2v1z"/>' +
      '</svg></span>'
    : '';

  var teamTextHtml;
  if (hoverHtml) {
    teamTextHtml = '<span class="lt-hover-wrap lt-team-text" tabindex="0" title="' +
      _ltEscapeHtml(team.text) + '">' + trophyHtml + _ltEscapeHtml(team.text) + hoverHtml + '</span>';
  } else {
    teamTextHtml = '<span class="lt-team-text" title="' + _ltEscapeHtml(team.text) + '">' +
      trophyHtml + _ltEscapeHtml(team.text) + '</span>';
  }

  return '<div class="' + classes.join(' ') + '"' + playerAttr +
    ' style="height:' + dims.teamHeight + 'px; min-height:' + dims.teamHeight + 'px; font-size:' + (0.93 * dims.fontScale) + 'rem">' +
    '<div class="lt-team-content">' +
    teamTextHtml +
    '<span class="lt-score-pill"' + scoreAttrs + '>' + _ltEscapeHtml(scoreText) + '</span>' +
    '</div></div>';
}

/**
 * Create a complete match card element.
 *
 * If a URL is available for this match in matchUrls, it becomes
 * a clickable <a> tag. Otherwise it's a <div>.
 */
function createMatchEl(match, dims, options, matchUrls) {
  // Look up URL by ref first, then by UUID
  var url = matchUrls[match.ref] || matchUrls[match.id] || null;
  var tagName = url ? 'a' : 'div';
  var el = document.createElement(tagName);
  var statusMeta = _ltGetMatchStatusMetaFromMatch(match);
  var matchRefDisplay = _ltFormatMatchRefDisplay(match.ref);
  el.className = 'lt-match' + (url ? ' clickable' : '');
  el.style.left = match.geom.boxLeft + 'px';
  el.style.top = match.geom.boxTop + 'px';
  el.style.width = dims.matchWidth + 'px';
  el.style.height = getMatchHeight(dims) + 'px';
  el.style.padding = dims.padding + 'px';
  el.style.gap = dims.rowGap + 'px';
  el.style.gridTemplateRows = dims.labelHeight + 'px ' + dims.teamHeight + 'px ' + dims.teamHeight + 'px';
  el.setAttribute('data-match-ref', match.ref);
  el.setAttribute('data-match-id', match.id);
  el.setAttribute('data-players', _ltPackPlayerData(match.playerKeys));
  el.title = matchRefDisplay + ' - ' + statusMeta.label;
  el.setAttribute('role', 'listitem');

  // Build descriptive ARIA label with contestant names
  var topName = (match.topResolved && match.topResolved.entrant && match.topResolved.entrant.type === 'player')
    ? match.topResolved.entrant.label : _t('tbd', 'TBD');
  var botName = (match.bottomResolved && match.bottomResolved.entrant && match.bottomResolved.entrant.type === 'player')
    ? match.bottomResolved.entrant.label : _t('tbd', 'TBD');
  el.setAttribute('aria-label', matchRefDisplay + ': ' + topName + ' ' + _t('vs', 'vs') + ' ' + botName + ' (' + statusMeta.label + ')');

  if (url) {
    el.href = url;
    el.setAttribute('aria-label', _t('openMatch', 'Open') + ' ' + matchRefDisplay);
  }

  if (statusMeta.key === 'open') el.className += ' match-open';
  if (statusMeta.key === 'pending') el.className += ' match-pending';
  if (statusMeta.key === 'auto') el.className += ' match-auto';
  if (statusMeta.key === 'done') el.className += ' match-done';
  el.setAttribute('data-match-status', statusMeta.key);

  var stageText = options.stageText
    ? '<span class="lt-match-stage" title="' + _ltEscapeHtml(options.stageText) + '">' + _ltEscapeHtml(options.stageText) + '</span>'
    : '';
  var statusTag = '<span class="lt-match-status lt-match-status--' + _ltEscapeHtml(statusMeta.key) + '" title="' + _t('status', 'Status') + ': ' + _ltEscapeHtml(statusMeta.label) + '">' + _ltEscapeHtml(statusMeta.label) + '</span>';

  el.innerHTML =
    '<div class="lt-match-label" style="font-size:' + (0.72 * dims.fontScale) + 'rem">' +
    '<span class="lt-match-ref-wrap" title="' + _ltEscapeHtml(matchRefDisplay) + ' - ' + _ltEscapeHtml(statusMeta.label) + '">' +
    '<span class="lt-match-state-dot lt-match-state-dot--' + _ltEscapeHtml(statusMeta.key) + '" aria-hidden="true"></span>' +
    '<span class="lt-match-ref-text">' + _ltEscapeHtml(matchRefDisplay) + '</span>' +
    '</span>' +
    '<span class="lt-match-meta">' + stageText + statusTag + '</span>' +
    '</div>' +
    buildTeamRow(match.topResolved.entrant, match.winnerIndex === 1, match.scores[0], dims, match.ref, options.hoverData, match.topPlacement || null, match.trophyMatch) +
    buildTeamRow(match.bottomResolved.entrant, match.winnerIndex === 2, match.scores[1], dims, match.ref, options.hoverData, match.bottomPlacement || null, match.trophyMatch);

  return el;
}


/* ===================================================================
 *  Source badges (DE bracket cross-bracket indicators)
 * =================================================================== */

/**
 * Create a source badge element indicating where a contestant comes from
 * (e.g. "Loser of WB R2 M1" appearing in the losers bracket).
 */
function createExternalSourceBadge(options) {
  if (!shouldShowExternalSource(options.sourceRef, options.currentBracket)) return null;

  var labelText = _ltGetSourceLabel(options.sourceRef, options.sourceOutcome, options.currentBracket);
  var statusMeta = options.settings.showSourceStatusInBadge ? _ltGetMatchStatusFromMap(options.sourceRef, options.matchMap) : null;
  var statusText = statusMeta ? statusMeta.label : '';

  var badge = document.createElement('div');
  var entrant = options.targetEntrant;
  var playerKey = (entrant && entrant.type === 'player') ? _ltNormalizePlayer(entrant.name) : '';
  var gapLeft = options.x - options.dims.connectorGap;
  var maxWidth = Math.max(100, options.dims.connectorGap - 16);
  var pillWidth = Math.min(_ltEstimateSourceLabelWidth(labelText, statusText, options.dims), maxWidth);
  var startX = Math.max(8, gapLeft + (options.dims.connectorGap - pillWidth) / 2);
  var routeId = _ltBuildRouteId(options.sourceRef, options.targetRef, options.slotName);

  badge.className = 'lt-incoming-source' + (options.sourceOutcome === 'loser' ? ' is-loser' : '');
  badge.style.left = startX + 'px';
  badge.style.top = (options.y - (options.dims.sourceBadgeHeight / 2)) + 'px';
  badge.style.width = pillWidth + 'px';
  badge.style.height = options.dims.sourceBadgeHeight + 'px';
  badge.setAttribute('data-players', _ltPackPlayerData(playerKey ? [playerKey] : []));
  badge.setAttribute('data-route', routeId);
  badge.setAttribute('data-source-ref', options.sourceRef);
  badge.setAttribute('data-target-ref', options.targetRef);
  badge.setAttribute('role', 'button');
  badge.tabIndex = 0;
  badge.title = (options.sourceOutcome === 'loser' ? _t('loser', 'Loser') + ' ' + _t('of', 'of') + ' ' : _t('winner', 'Winner') + ' ' + _t('of', 'of') + ' ') +
    _ltFormatMatchRefDisplay(options.sourceRef) +
    (statusText ? ' - ' + statusText : '') +
    ' \u2013 ' + _t('clickToJump', 'click to jump to source match');
  badge.setAttribute('aria-label', labelText + (statusText ? ' ' + statusText : '') + ' \u2013 ' + _t('jumpTo', 'jump to') + ' ' + _ltFormatMatchRefDisplay(options.sourceRef));

  var dotHtml = statusMeta
    ? '<span class="lt-incoming-pill-state-dot lt-incoming-pill-dot--' + _ltEscapeHtml(statusMeta.key) + '" aria-hidden="true"></span>'
    : '';

  badge.innerHTML =
    '<span class="lt-incoming-pill">' +
    dotHtml +
    '<span class="lt-incoming-pill-label" title="' + _ltEscapeHtml(labelText) + '">' + _ltEscapeHtml(labelText) + '</span>' +
    '</span>';

  return {
    badge: badge,
    lineStartX: startX + pillWidth + 6,
    routeId: routeId,
    playerKey: playerKey,
    sourceRef: options.sourceRef,
    targetRef: options.targetRef
  };
}

/**
 * Attach external source badges and their connector lines to a match.
 */
function appendExternalSources(container, svg, match, dims, currentBracket, matchMap, settings) {
  // Always target each source badge at its specific team-slot Y position
  // so the external feeder line is visually distinct from the internal
  // bracket connector (which enters at centerY).
  var topY = match.geom.teamTopY;
  var bottomY = match.geom.teamBottomY;

  var topBadge = createExternalSourceBadge({
    sourceRef: match.topResolved.sourceRef,
    sourceOutcome: match.topResolved.sourceOutcome,
    targetEntrant: match.topResolved.entrant,
    currentBracket: currentBracket,
    x: match.geom.boxLeft,
    y: topY,
    dims: dims,
    targetRef: match.ref,
    slotName: 'top',
    matchMap: matchMap,
    settings: settings
  });

  var bottomBadge = createExternalSourceBadge({
    sourceRef: match.bottomResolved.sourceRef,
    sourceOutcome: match.bottomResolved.sourceOutcome,
    targetEntrant: match.bottomResolved.entrant,
    currentBracket: currentBracket,
    x: match.geom.boxLeft,
    y: bottomY,
    dims: dims,
    targetRef: match.ref,
    slotName: 'bottom',
    matchMap: matchMap,
    settings: settings
  });

  var entries = [topBadge, bottomBadge];
  var yPositions = [topY, bottomY];
  var idx;
  for (idx = 0; idx < entries.length; idx++) {
    if (!entries[idx]) continue;
    container.appendChild(entries[idx].badge);
    _ltAddSourceConnector(svg, {
      startX: entries[idx].lineStartX,
      endX: match.geom.boxLeft,
      y: yPositions[idx],
      players: entries[idx].playerKey ? [entries[idx].playerKey] : [],
      routeId: entries[idx].routeId,
      sourceRef: entries[idx].sourceRef,
      targetRef: entries[idx].targetRef
    });
  }
}


/* ===================================================================
 *  Bracket panning (drag-to-scroll)
 * =================================================================== */

/**
 * Enable pointer-based horizontal panning on the bracket scroll container.
 */
function _ltEnableBracketPan(scrollEl) {
  var canvas = scrollEl.querySelector('.lt-bracket-canvas');
  if (!canvas) return;

  var interactiveSelector = '.lt-match, .lt-incoming-source, .lt-team, a, button, select, input, label';
  var needsPan = canvas.scrollWidth > (scrollEl.clientWidth + 8);
  var useNativeTouchPan = window.matchMedia && window.matchMedia('(hover: none), (pointer: coarse)').matches;

  if (needsPan) {
    scrollEl.className += ' is-pannable';
  }

  if (!needsPan) return;
  if (useNativeTouchPan) return;

  var pointerId = null;
  var startX = 0;
  var startScrollLeft = 0;
  var moved = false;

  scrollEl.addEventListener('pointerdown', function(event) {
    if (event.button !== 0) return;
    if (event.target.closest && event.target.closest(interactiveSelector)) return;

    pointerId = event.pointerId;
    moved = false;
    startX = event.clientX;
    startScrollLeft = scrollEl.scrollLeft;
    scrollEl.className += ' is-panning';
    if (scrollEl.setPointerCapture) scrollEl.setPointerCapture(pointerId);
  });

  scrollEl.addEventListener('pointermove', function(event) {
    if (pointerId !== event.pointerId) return;
    var delta = event.clientX - startX;
    if (Math.abs(delta) > 3) moved = true;
    scrollEl.scrollLeft = startScrollLeft - delta;
  });

  var stopPan = function(event) {
    if (pointerId !== event.pointerId) return;
    scrollEl.className = scrollEl.className.replace(/\bis-panning\b/g, '').trim();
    if (scrollEl.hasPointerCapture && scrollEl.hasPointerCapture(pointerId)) {
      scrollEl.releasePointerCapture(pointerId);
    }
    if (moved) {
      event.preventDefault();
    }
    pointerId = null;
  };

  scrollEl.addEventListener('pointerup', stopPan);
  scrollEl.addEventListener('pointercancel', stopPan);
  scrollEl.addEventListener('lostpointercapture', function() {
    pointerId = null;
    scrollEl.className = scrollEl.className.replace(/\bis-panning\b/g, '').trim();
  });
}


/* ===================================================================
 *  Section stats
 * =================================================================== */

/**
 * Compute aggregate statistics for a bracket section.
 *
 * Iterates all matches across the supplied rounds and tallies:
 *   - matchCount:       total number of matches
 *   - completedCount:   matches that are done or auto-advanced
 *   - completionPct:    integer 0-100 (0 when no matches)
 *   - contestantCount:  unique real contestants (by player key)
 *
 * @param {Array} rounds  Array of round objects, each with a `matches` array.
 * @returns {Object}  { matchCount, completedCount, completionPct, contestantCount }
 */
function computeSectionStats(rounds) {
  var matchCount = 0;
  var completedCount = 0;
  var seen = {};
  var contestantCount = 0;
  var i, j, round, match, k;

  for (i = 0; i < rounds.length; i++) {
    round = rounds[i];
    for (j = 0; j < round.matches.length; j++) {
      match = round.matches[j];
      if (match.isDead) continue;
      matchCount++;
      if (match.isComplete || match.isAutoAdvanced) {
        completedCount++;
      }
      for (k = 0; k < match.playerKeys.length; k++) {
        if (match.playerKeys[k] && !seen[match.playerKeys[k]]) {
          seen[match.playerKeys[k]] = true;
          contestantCount++;
        }
      }
    }
  }

  return {
    matchCount: matchCount,
    completedCount: completedCount,
    completionPct: matchCount > 0 ? Math.round((completedCount / matchCount) * 100) : 0,
    contestantCount: contestantCount
  };
}

/**
 * Build the HTML for a stats bar from computed section stats.
 *
 * @param {Object} stats  Output of computeSectionStats().
 * @returns {string}  HTML string for the stats bar.
 */
function _ltBuildStatsBarHtml(stats) {
  return '<div class="lt-stats-bar" role="status" aria-label="' + _t('sectionStatistics', 'Section statistics') + '">' +
    '<span class="lt-stats-chip" title="' + _t('totalMatches', 'Total matches') + '">' +
      _ltEscapeHtml(stats.matchCount) + ' ' + _tp(stats.matchCount, 'matchCountSingular', 'matchCountPlural') +
    '</span>' +
    '<span class="lt-stats-chip" title="' + _t('completionPercentage', 'Completion percentage') + '">' +
      _ltEscapeHtml(stats.completionPct) + '% ' + _t('pctComplete', 'complete') +
    '</span>' +
    '<span class="lt-stats-chip" title="' + _t('uniqueContestants', 'Unique contestants') + '">' +
      _ltEscapeHtml(stats.contestantCount) + ' ' + _tp(stats.contestantCount, 'contestantSingular', 'contestantPlural') +
    '</span>' +
  '</div>';
}


/* ===================================================================
 *  Section-level rendering
 * =================================================================== */

/**
 * Create a view-select dropdown for filtering DE bracket sections.
 *
 * Options: All (default), Winners only, Losers only, Top 8.
 * Fires a 'change' event consumed by applyMode() in lan_tournament.js,
 * which delegates to the bracket instance's setViewMode() for re-render.
 *
 * @param {HTMLElement} rootEl       The bracket root container.
 * @param {string}      currentMode  Active view mode to pre-select.
 * @returns {HTMLElement}  The dropdown wrapper element.
 */
function _ltCreateViewSelect(rootEl, currentMode) {
  var wrapper = document.createElement('div');
  wrapper.className = 'lt-view-select';

  var label = document.createElement('label');
  label.className = 'lt-view-select-label';
  label.textContent = _t('view', 'View');

  var select = document.createElement('select');
  select.className = 'view-select';
  select.setAttribute('aria-label', _t('filterBracketView', 'Filter bracket view'));

  var options = [
    { value: 'full', text: _t('allBrackets', 'All brackets') },
    { value: 'winners', text: _t('winnersOnly', 'Winners only') },
    { value: 'losers', text: _t('losersOnly', 'Losers only') },
    { value: 'top8', text: _t('top8', 'Top 8') }
  ];

  var i, opt;
  for (i = 0; i < options.length; i++) {
    opt = document.createElement('option');
    opt.value = options[i].value;
    opt.textContent = options[i].text;
    if (currentMode && options[i].value === currentMode) {
      opt.selected = true;
    }
    select.appendChild(opt);
  }

  wrapper.appendChild(label);
  wrapper.appendChild(select);

  // Mark the root as a bracket-shell so initBracketViewSwitcher can find it
  if (!rootEl.classList.contains('bracket-shell')) {
    rootEl.classList.add('bracket-shell');
  }

  return wrapper;
}

/**
 * Render a complete bracket section (WB or LB) with round titles,
 * match cards, connectors, and source badges.
 */
function renderBracketSection(options) {
  // Pre-compute P3 source badge width so the connector gap is wide enough
  // for inline third-place source pills that appear only in SE brackets.
  var fittedOpts = { skipExternalSources: !options.isDE };
  if (!options.isDE && options.thirdPlaceMatches && options.thirdPlaceMatches.length > 0) {
    var baseDims = getBaseDims();
    var p3Round = { bracket: 'P3', displayBracket: 'P3', matches: options.thirdPlaceMatches };
    fittedOpts.minSourceBadgeWidth = _ltGetMaxSourceBadgeWidth([p3Round], baseDims, options.matchMap, options.settings);
  }
  var dims = getFittedDims(options.rounds.length, options.rounds, options.matchMap, options.settings, options.appEl, fittedOpts);
  var layout = layoutRounds(options.rounds, options.fieldSize, dims);
  var i, round, j, match, stageText;

  // Compute inline P3 geometry (SE only)
  var inlineP3Matches = [];
  if (options.thirdPlaceMatches && options.thirdPlaceMatches.length > 0 && !options.isDE) {
    var finalRound = options.rounds[options.rounds.length - 1];
    var gfMatch = finalRound.matches[finalRound.matches.length - 1];
    var p3Gap = dims.slotHeight;  // standard inter-match gap
    var p3CenterY = gfMatch.geom.boxBottom + p3Gap + (layout.matchHeight / 2);
    var teamOffsets = getTeamOffsets(dims);

    for (var pi = 0; pi < options.thirdPlaceMatches.length; pi++) {
      var p3m = options.thirdPlaceMatches[pi];
      p3m.geom = {
        centerY: p3CenterY,
        boxLeft: finalRound.boxLeft,
        boxRight: finalRound.boxRight,
        boxTop: p3CenterY - (layout.matchHeight / 2),
        boxBottom: p3CenterY + (layout.matchHeight / 2),
        teamTopY: (p3CenterY - (layout.matchHeight / 2)) + teamOffsets.top,
        teamBottomY: (p3CenterY - (layout.matchHeight / 2)) + teamOffsets.bottom
      };
      inlineP3Matches.push(p3m);
      p3CenterY += layout.matchHeight + p3Gap;
    }

    // Extend canvas dimensions
    var p3Bottom = inlineP3Matches[inlineP3Matches.length - 1].geom.boxBottom;
    var extraHeight = p3Bottom - (layout.headerOffset + layout.bracketHeight);
    if (extraHeight > 0) {
      layout.height += extraHeight + dims.padding;
    }
  }

  var stats = computeSectionStats(options.rounds);
  var statsBarHtml = _ltBuildStatsBarHtml(stats);

  var section = document.createElement('section');
  section.className = 'lt-bracket-section';
  if (options.bracketView) {
    section.setAttribute('data-bracket-view', options.bracketView);
  }
  section.innerHTML =
    '<div class="lt-bracket-section-head">' +
    '<h2>' + _ltEscapeHtml(options.title) + '</h2>' +
    statsBarHtml +
    '<span>' + _ltEscapeHtml(options.subtitle) + '</span>' +
    '</div>' +
    '<div class="lt-bracket-section-body">' +
    '<div class="lt-bracket-scroll">' +
    '<div class="lt-bracket-canvas" role="list" aria-label="' + _ltEscapeHtml(options.title) + ' matches"></div>' +
    '</div></div>';

  var scroll = section.querySelector('.lt-bracket-scroll');
  var canvas = section.querySelector('.lt-bracket-canvas');
  canvas.style.width = layout.width + 'px';
  canvas.style.height = layout.height + 'px';

  var svg = _ltCreateSvg(layout.width, layout.height);
  drawConnectorLines(svg, options.rounds, dims);
  canvas.appendChild(svg);

  for (i = 0; i < options.rounds.length; i++) {
    round = options.rounds[i];
    canvas.appendChild(createRoundTitle(round, dims));
    for (j = 0; j < round.matches.length; j++) {
      match = round.matches[j];
      stageText = round.title;
      // SE brackets don't need source badges on regular matches — the bracket
      // structure is self-explanatory.  P3 inline matches (below) keep theirs.
      if (options.isDE) {
        appendExternalSources(canvas, svg, match, dims, round.displayBracket || match.bracket, options.matchMap, options.settings);
      }
      canvas.appendChild(createMatchEl(match, dims, { stageText: stageText, hoverData: options.hoverData }, options.matchUrls));
    }
  }

  // Render inline P3 matches
  for (i = 0; i < inlineP3Matches.length; i++) {
    match = inlineP3Matches[i];
    appendExternalSources(canvas, svg, match, dims, 'P3', options.matchMap, options.settings);
    var p3El = createMatchEl(match, dims, { stageText: _t('thirdPlace', '3rd Place'), hoverData: options.hoverData }, options.matchUrls);
    p3El.className += ' lt-p3-inline';
    canvas.appendChild(p3El);
  }

  requestAnimationFrame(function() { _ltEnableBracketPan(scroll); });
  return section;
}

/**
 * Render the finals/extra matches section (reset game, 3rd place, etc.)
 * in a non-bracket grid layout.
 */
function renderFinalsSection(finalMatches, matchMap, settings, matchUrls, appEl, hoverData) {
  var dims = getFittedDims(1, [], matchMap, settings, appEl);
  var matchHeight = getMatchHeight(dims);
  var i, match, card, wrap, matchEl;

  var finalsRounds = [{ matches: finalMatches }];
  var finalsStats = computeSectionStats(finalsRounds);
  var finalsStatsBarHtml = _ltBuildStatsBarHtml(finalsStats);

  var section = document.createElement('section');
  section.className = 'lt-bracket-section';
  section.innerHTML =
    '<div class="lt-bracket-section-head">' +
    '<h2>' + _t('additionalMatches', 'Additional Matches') + '</h2>' +
    finalsStatsBarHtml +
    '<span>' + _t('optionalResetGame', 'Optional reset game and additional matches') + '</span>' +
    '</div>' +
    '<div class="lt-bracket-section-body">' +
    '<div class="lt-finals-grid"></div>' +
    '</div>';

  var grid = section.querySelector('.lt-finals-grid');

  for (i = 0; i < finalMatches.length; i++) {
    match = finalMatches[i];
    card = document.createElement('div');
    card.className = 'lt-final-card';

    var titleText = match.title || _ltFormatMatchRefDisplay(match.ref);
    var subtitleText = match.subtitle || '';

    card.innerHTML =
      '<h3>' + _ltEscapeHtml(titleText) + '</h3>' +
      '<p>' + _ltEscapeHtml(subtitleText) + '</p>' +
      '<div class="lt-final-match-wrap"></div>';

    wrap = card.querySelector('.lt-final-match-wrap');

    _ltAssignCardGeom(match, dims);
    matchEl = createMatchEl(match, dims, { stageText: titleText, hoverData: hoverData }, matchUrls);
    matchEl.className += ' static';
    matchEl.style.height = matchHeight + 'px';
    wrap.appendChild(matchEl);
    grid.appendChild(card);
  }

  return section;
}

/**
 * Render the third-place match section as a standalone card.
 * Gated by settings.showThirdPlaceMatch in the render() caller.
 *
 * @param {Array}  thirdPlaceMatches - parsed P3 matches from parseBracketData
 * @param {Object} matchMap          - ref -> match lookup
 * @param {Object} settings          - renderer settings
 * @param {Object} matchUrls         - ref -> URL lookup
 * @param {Element} appEl            - bracket root element
 * @param {Object} hoverData         - hover/tooltip data
 * @returns {Element} section DOM node
 */
function renderThirdPlaceSection(thirdPlaceMatches, matchMap, settings, matchUrls, appEl, hoverData) {
  var dims = getFittedDims(1, [], matchMap, settings, appEl);
  var matchHeight = getMatchHeight(dims);
  var i, match, card, wrap, matchEl;

  var p3Rounds = [{ matches: thirdPlaceMatches }];
  var p3Stats = computeSectionStats(p3Rounds);
  var p3StatsBarHtml = _ltBuildStatsBarHtml(p3Stats);

  var section = document.createElement('section');
  section.className = 'lt-bracket-section lt-p3-section';
  section.setAttribute('data-bracket-view', 'third-place');
  section.innerHTML =
    '<div class="lt-bracket-section-head">' +
    '<h2>' + _t('thirdPlaceMatch', '3rd Place Match') + '</h2>' +
    p3StatsBarHtml +
    '<span>' + _t('semifinalLosersCompete', 'Semifinal losers compete for third place') + '</span>' +
    '</div>' +
    '<div class="lt-bracket-section-body">' +
    '<div class="lt-finals-grid"></div>' +
    '</div>';

  var grid = section.querySelector('.lt-finals-grid');

  for (i = 0; i < thirdPlaceMatches.length; i++) {
    match = thirdPlaceMatches[i];
    card = document.createElement('div');
    card.className = 'lt-final-card lt-p3-card';

    var titleText = match.title || _ltFormatMatchRefDisplay(match.ref);
    var subtitleText = match.subtitle || _t('loserSfVsLoserSf', 'Loser SF 1 vs Loser SF 2');

    card.innerHTML =
      '<h3>' + _ltEscapeHtml(titleText) + '</h3>' +
      '<p>' + _ltEscapeHtml(subtitleText) + '</p>' +
      '<div class="lt-final-match-wrap"></div>';

    wrap = card.querySelector('.lt-final-match-wrap');

    _ltAssignCardGeom(match, dims);
    matchEl = createMatchEl(match, dims, { stageText: titleText, hoverData: hoverData }, matchUrls);
    matchEl.className += ' static';
    matchEl.style.height = matchHeight + 'px';
    wrap.appendChild(matchEl);
    grid.appendChild(card);
  }

  return section;
}


/* ===================================================================
 *  Hover highlight system
 * =================================================================== */

/**
 * Clear all active highlight classes from the bracket root.
 */
function _ltClearHighlights(rootEl) {
  rootEl.className = rootEl.className.replace(/\blt-bracket-focus-mode\b/g, '').trim();
  var highlighted = rootEl.querySelectorAll('.has-active-player, .has-active-route');
  var i;
  for (i = 0; i < highlighted.length; i++) {
    highlighted[i].className = highlighted[i].className
      .replace(/\bhas-active-player\b/g, '')
      .replace(/\bhas-active-route\b/g, '')
      .trim();
  }
}

/**
 * Mark elements whose data-players attribute contains the given player key.
 */
function _ltMarkByPlayers(rootEl, selector, className, playerKey) {
  var needle = '|' + playerKey + '|';
  var els = rootEl.querySelectorAll(selector);
  var i;
  for (i = 0; i < els.length; i++) {
    if ((els[i].getAttribute('data-players') || '').indexOf(needle) !== -1) {
      els[i].className += ' ' + className;
    }
  }
}

/**
 * Mark match elements by data-match-ref.
 */
function _ltMarkMatchByRef(rootEl, ref, className) {
  var els = rootEl.querySelectorAll('.lt-match[data-match-ref]');
  var i;
  for (i = 0; i < els.length; i++) {
    if (els[i].getAttribute('data-match-ref') === ref) {
      els[i].className += ' ' + className;
    }
  }
}

/**
 * Highlight all elements associated with a given player.
 */
function _ltSetActivePlayer(rootEl, playerKey) {
  _ltClearHighlights(rootEl);
  if (!playerKey) return;

  rootEl.className += ' lt-bracket-focus-mode';

  _ltMarkByPlayers(rootEl, '.lt-match[data-players]', 'has-active-player', playerKey);

  var teams = rootEl.querySelectorAll('.lt-team[data-player]');
  var i;
  for (i = 0; i < teams.length; i++) {
    if (teams[i].getAttribute('data-player') === playerKey) {
      teams[i].className += ' has-active-player';
    }
  }

  _ltMarkByPlayers(rootEl, '.lt-connector[data-players]', 'has-active-player', playerKey);
  _ltMarkByPlayers(rootEl, '.lt-source-connector[data-players], .lt-incoming-source[data-players]', 'has-active-player', playerKey);
}

/**
 * Highlight a specific source route (badge -> match connection).
 */
function _ltSetActiveRoute(rootEl, routeId, sourceRef, targetRef) {
  _ltClearHighlights(rootEl);
  if (!routeId) return;

  rootEl.className += ' lt-bracket-focus-mode';

  var els = rootEl.querySelectorAll('.lt-incoming-source[data-route], .lt-source-connector[data-route]');
  var i;
  for (i = 0; i < els.length; i++) {
    if (els[i].getAttribute('data-route') === routeId) {
      els[i].className += ' has-active-route';
    }
  }

  var refs = [sourceRef, targetRef];
  for (i = 0; i < refs.length; i++) {
    if (refs[i]) _ltMarkMatchByRef(rootEl, refs[i], 'has-active-route');
  }
}

/**
 * Highlight a match and all routes touching it.
 */
function _ltSetActiveMatchRef(rootEl, ref) {
  _ltClearHighlights(rootEl);
  if (!ref) return;

  rootEl.className += ' lt-bracket-focus-mode';
  _ltMarkMatchByRef(rootEl, ref, 'has-active-route');

  var els = rootEl.querySelectorAll('.lt-incoming-source[data-source-ref], .lt-incoming-source[data-target-ref], .lt-source-connector[data-source-ref], .lt-source-connector[data-target-ref]');
  var i, sourceRef, targetRef, matchRefs, k;
  for (i = 0; i < els.length; i++) {
    sourceRef = els[i].getAttribute('data-source-ref');
    targetRef = els[i].getAttribute('data-target-ref');
    if (sourceRef === ref || targetRef === ref) {
      els[i].className += ' has-active-route';
      matchRefs = [sourceRef, targetRef];
      for (k = 0; k < matchRefs.length; k++) {
        if (matchRefs[k]) _ltMarkMatchByRef(rootEl, matchRefs[k], 'has-active-route');
      }
    }
  }
}

/**
 * Find a match element by its ref within the root.
 */
function _ltFindMatchByRef(rootEl, ref) {
  var els = rootEl.querySelectorAll('.lt-match[data-match-ref]');
  var i;
  for (i = 0; i < els.length; i++) {
    if (els[i].getAttribute('data-match-ref') === ref) return els[i];
  }
  return null;
}

/**
 * Scroll to and flash a match element.
 */
function _ltActivateAndScrollToMatchRef(rootEl, ref) {
  var target = _ltFindMatchByRef(rootEl, ref);
  if (!target) return false;
  var reduceMotion = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  _ltSetActiveMatchRef(rootEl, ref);
  target.className = target.className.replace(/\bflash-target\b/g, '').trim();
  void target.offsetWidth;  // force reflow
  target.className += ' flash-target';
  target.scrollIntoView({ behavior: reduceMotion ? 'auto' : 'smooth', block: 'center', inline: 'center' });
  clearTimeout(rootEl._ltFlashTimer);
  rootEl._ltFlashTimer = setTimeout(function() {
    target.className = target.className.replace(/\bflash-target\b/g, '').trim();
  }, 1400);
  return true;
}

/**
 * Bind hover/focus/click highlight handlers to the bracket root.
 */
function bindHoverHighlight(rootEl, appEl, scrollToMatchRefFn) {
  var activeKey = '';

  var resolveTarget = function(node) {
    if (!node || !appEl.contains(node)) return null;

    var team = node.closest ? node.closest('.lt-team[data-player]') : null;
    if (team) return { type: 'player', key: team.getAttribute('data-player') };

    var scorePill = node.closest ? node.closest('.lt-score-pill[data-player], .lt-score-pill[data-match-ref]') : null;
    if (scorePill) {
      if (scorePill.getAttribute('data-player')) {
        return { type: 'player', key: scorePill.getAttribute('data-player') };
      }
      if (scorePill.getAttribute('data-match-ref')) {
        return { type: 'score-match', key: scorePill.getAttribute('data-match-ref') };
      }
    }

    var source = node.closest ? node.closest('.lt-incoming-source[data-route]') : null;
    if (source) {
      return {
        type: 'route',
        key: source.getAttribute('data-route'),
        sourceRef: source.getAttribute('data-source-ref'),
        targetRef: source.getAttribute('data-target-ref')
      };
    }

    // Hovering anywhere on an inline P3 match card triggers match-ref
    // tracing so the loser-path from SF matches lights up.
    var p3Match = node.closest ? node.closest('.lt-p3-inline[data-match-ref]') : null;
    if (p3Match) {
      return { type: 'score-match', key: p3Match.getAttribute('data-match-ref') };
    }

    return null;
  };

  var applyTarget = function(target) {
    var nextKey = target ? (target.type + ':' + target.key) : '';
    if (nextKey === activeKey) return;
    activeKey = nextKey;

    if (!target) {
      _ltClearHighlights(rootEl);
      return;
    }

    if (target.type === 'player') {
      _ltSetActivePlayer(rootEl, target.key);
      return;
    }

    if (target.type === 'route') {
      _ltSetActiveRoute(rootEl, target.key, target.sourceRef, target.targetRef);
      return;
    }

    if (target.type === 'score-match') {
      _ltSetActiveMatchRef(rootEl, target.key);
    }
  };

  appEl.onmousemove = function(event) { applyTarget(resolveTarget(event.target)); };
  appEl.onfocusin = function(event) { applyTarget(resolveTarget(event.target)); };
  appEl.onmouseleave = function() {
    activeKey = '';
    _ltClearHighlights(rootEl);
  };
  appEl.onfocusout = function(event) {
    if (event.relatedTarget && appEl.contains(event.relatedTarget)) return;
    activeKey = '';
    _ltClearHighlights(rootEl);
  };
  appEl.onclick = function(event) {
    var source = event.target.closest ? event.target.closest('.lt-incoming-source[data-source-ref]') : null;
    if (!source) return;
    event.preventDefault();
    event.stopPropagation();
    if (scrollToMatchRefFn) scrollToMatchRefFn(source.getAttribute('data-source-ref'));
  };
  appEl.onkeydown = function(event) {
    if (event.key !== 'Enter' && event.key !== ' ') return;
    var source = event.target.closest ? event.target.closest('.lt-incoming-source[data-source-ref]') : null;
    if (!source) return;
    event.preventDefault();
    if (scrollToMatchRefFn) scrollToMatchRefFn(source.getAttribute('data-source-ref'));
  };
}


/* ===================================================================
 *  Keyboard navigation
 * =================================================================== */

/**
 * Enable arrow-key navigation between match cards within bracket sections.
 *
 * Arrow Up / Arrow Left:  move focus to previous match (wraps).
 * Arrow Down / Arrow Right: move focus to next match (wraps).
 * Escape: clear focus and dismiss any active highlight.
 *
 * Navigation is scoped to the nearest `.lt-bracket-section` so arrow
 * keys traverse within one bracket side at a time.
 *
 * @param {HTMLElement} rootEl  The bracket root container.
 */
function _ltBindKeyboardNav(rootEl) {
  rootEl.addEventListener('keydown', function(event) {
    var active = document.activeElement;
    if (!active || !active.classList.contains('lt-match')) return;

    var section = active.closest('.lt-bracket-section');
    if (!section) section = rootEl;

    var matches = [];
    var all = section.querySelectorAll('.lt-match[tabindex]');
    for (var i = 0; i < all.length; i++) matches.push(all[i]);
    var idx = matches.indexOf(active);
    if (idx === -1) return;

    var target = null;
    if (event.key === 'ArrowDown' || event.key === 'ArrowRight') {
      target = matches[idx + 1] || matches[0];
    } else if (event.key === 'ArrowUp' || event.key === 'ArrowLeft') {
      target = matches[idx - 1] || matches[matches.length - 1];
    } else if (event.key === 'Escape') {
      active.blur();
      _ltClearHighlights(rootEl);
      return;
    } else {
      return;
    }

    event.preventDefault();
    if (target) {
      target.focus();
      target.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    }
  });
}


/* ===================================================================
 *  GF M2 / bracket reset helpers
 * =================================================================== */

/**
 * Determine whether a match is a Grand Finals bracket-reset game (GF M2+).
 * In double-elimination, GF M1 is the initial grand final; GF M2 (the
 * "bracket reset") only occurs if the losers-bracket winner takes GF M1.
 *
 * @param {Object} match  Internal match object produced by parseBracketData.
 * @returns {boolean}
 */
function _ltIsGFResetMatch(match) {
  if (!match || match.bracket !== 'GF') return false;
  var parsed = _ltParseMatchRef(match.ref);
  return !!(parsed && parsed.match >= 2);
}

/**
 * Render a bracket-reset card for a GF M2 match.  Wraps the standard
 * match element in a final-card with a prominent "Bracket Reset" label
 * and applies the `bracket-reset` CSS modifier to the match element.
 *
 * @param {Object} match       The internal match object.
 * @param {Object} matchMap    The global match-ref -> match index.
 * @param {Object} settings    Bracket renderer settings.
 * @param {Object} matchUrls   Match URL map.
 * @param {Element} appEl      Root bracket element.
 * @param {Object} hoverData   Hover / highlight data.
 * @returns {Element}          A section element ready for DOM insertion.
 */
function _ltRenderBracketResetSection(match, matchMap, settings, matchUrls, appEl, hoverData) {
  var dims = getFittedDims(1, [], matchMap, settings, appEl);
  var matchHeight = getMatchHeight(dims);

  var resetRounds = [{ matches: [match] }];
  var resetStats = computeSectionStats(resetRounds);
  var resetStatsBarHtml = _ltBuildStatsBarHtml(resetStats);

  var section = document.createElement('section');
  section.className = 'lt-bracket-section';

  var gfM1Ref = 'GF M1';
  var gfM1 = matchMap[gfM1Ref] || null;
  var conditionNote = gfM1 && gfM1.isComplete && gfM1.winnerIndex
    ? ''
    : _t('playedOnlyIfLbWinner', 'Played only if the losers-bracket winner takes Grand Final Game 1.');

  section.innerHTML =
    '<div class="lt-bracket-section-head">' +
    '<h2>' + _t('bracketReset', 'Bracket Reset') + '</h2>' +
    resetStatsBarHtml +
    '<span>' + _ltEscapeHtml(conditionNote || _t('grandFinalDecisive', 'Grand Final \u2013 Decisive Game')) + '</span>' +
    '</div>' +
    '<div class="lt-bracket-section-body">' +
    '<div class="lt-finals-grid"></div>' +
    '</div>';

  var grid = section.querySelector('.lt-finals-grid');
  var card = document.createElement('div');
  card.className = 'lt-final-card lt-final-card--reset';

  var titleText = _t('grandFinalBracketReset', 'Grand Final \u2013 Bracket Reset');
  var subtitleText = _t('trueFinal', 'True final: both players enter 1\u20131 in the series');

  card.innerHTML =
    '<h3>' + _ltEscapeHtml(titleText) + '</h3>' +
    '<p>' + _ltEscapeHtml(subtitleText) + '</p>' +
    '<div class="lt-final-match-wrap"></div>';

  var wrap = card.querySelector('.lt-final-match-wrap');
  _ltAssignCardGeom(match, dims);
  var matchEl = createMatchEl(match, dims, { stageText: titleText, hoverData: hoverData }, matchUrls);
  matchEl.className += ' static bracket-reset';
  matchEl.style.height = matchHeight + 'px';
  wrap.appendChild(matchEl);
  grid.appendChild(card);

  return section;
}


/* ===================================================================
 *  View-mode round filtering (ported from Turniercss/turnier-shared.js)
 * =================================================================== */

/**
 * Filter WB rounds to Top 8 view: keep rounds with <= 4 matches or GF.
 * For a 16-slot bracket: drops R1 (8 matches), keeps R2-R4 (4/2/1).
 */
function _ltGetTop8WinnerRounds(rounds) {
  if (rounds.length <= 4) return rounds;
  return rounds.filter(function(round) {
    return round.bracket === 'GF' || round.matches.length <= 4;
  });
}

/**
 * Filter LB rounds to Top 8 view: keep rounds with <= 2 matches
 * or the last 4 rounds (whichever is more inclusive).
 */
function _ltGetTop8LoserRounds(rounds) {
  if (rounds.length <= 4) return rounds;
  return rounds.filter(function(round, index) {
    return round.matches.length <= 2 || index >= rounds.length - 4;
  });
}


/* ===================================================================
 *  Public API
 * =================================================================== */

/**
 * Create a bracket renderer instance.
 *
 * @param {Object} config
 *   config.rootId    - ID of the root container element
 *   config.data      - Parsed server JSON (or raw JSON to be parsed)
 *   config.matchUrls - Optional map of match_ref -> admin URL
 *   config.settings  - Optional settings overrides
 * @returns {Object}  { render(), destroy(), getMatchMap() }
 */
function createBracketInstance(config) {
  var rootEl = document.getElementById(config.rootId);
  if (!rootEl) return null;

  var settings = {
    showSourceStatusInBadge: true,
    showThirdPlaceMatch: true,
    useBracketReset: true
  };
  var key;
  if (config.settings) {
    for (key in config.settings) {
      if (config.settings.hasOwnProperty(key)) {
        settings[key] = config.settings[key];
      }
    }
  }

  var data = config.data || {};
  var matchUrls = config.matchUrls || data.match_urls || {};
  var parsed = null;
  var pendingScrollRef = '';
  var viewMode = 'full';

  function ensureParsed() {
    if (!parsed) {
      parsed = parseBracketData(data);
      _ltComputePlacements(parsed);
    }
    return parsed;
  }

  function scrollToMatchRef(ref) {
    if (!ref) return;
    if (_ltActivateAndScrollToMatchRef(rootEl, ref)) return;
    // If match not visible, could switch view — for now just try
    pendingScrollRef = ref;
    render();
  }

  function render() {
    var p = ensureParsed();
    var urls = p.matchUrls || {};
    var hoverData = p.hoverData || { seats: {}, team_members: {} };
    var i;

    // Merge in any URLs provided at instance level
    if (matchUrls) {
      for (i in matchUrls) {
        if (matchUrls.hasOwnProperty(i) && !urls[i]) {
          urls[i] = matchUrls[i];
        }
      }
    }

    _ltClearHighlights(rootEl);
    rootEl.innerHTML = '';

    if (!p.winnerRounds.length && !p.loserRounds.length && !p.finalMatches.length && !p.thirdPlaceMatches.length) {
      rootEl.innerHTML = '<div class="lt-empty-state">' + _t('noBracketData', 'No bracket data available.') + '</div>';
      return;
    }

    // Compute field size from the round with the most matches.
    // Standard brackets have max matches in R1, but play-in brackets
    // can have more matches in later rounds (e.g., 2 R1 play-ins
    // feeding into 4 R2 quarterfinals for a 9-contestant field).
    var fieldSize = 2;
    if (p.winnerRounds.length > 0) {
      var maxMatches = 0;
      for (var fi = 0; fi < p.winnerRounds.length; fi++) {
        if (p.winnerRounds[fi].matches.length > maxMatches) {
          maxMatches = p.winnerRounds[fi].matches.length;
        }
      }
      fieldSize = maxMatches * 2;
      if (fieldSize < 2) fieldSize = 2;
    }
    // Ensure power-of-2 for connector alignment
    fieldSize = Math.pow(2, Math.ceil(Math.log2(fieldSize)));

    // Apply view-mode round filtering
    var wbRounds = p.winnerRounds;
    var lbRounds = p.loserRounds;
    if (viewMode === 'top8') {
      wbRounds = _ltGetTop8WinnerRounds(wbRounds);
      lbRounds = _ltGetTop8LoserRounds(lbRounds);
    }
    var isDE = p.loserRounds.length > 0;
    var showWB = (viewMode === 'full' || viewMode === 'winners' || viewMode === 'top8');
    var showLB = (viewMode === 'full' || viewMode === 'losers' || viewMode === 'top8');
    var showFinals = (viewMode !== 'losers');

    // Insert view-select dropdown for DE brackets
    if (isDE) {
      rootEl.appendChild(_ltCreateViewSelect(rootEl, viewMode));
    }

    // Render winners bracket
    if (showWB && wbRounds.length > 0) {
      rootEl.appendChild(renderBracketSection({
        title: isDE ? _t('winnersBracket', 'Winners Bracket (WB)') : _t('bracket', 'Bracket'),
        subtitle: fieldSize + _t('slotField', '-slot field'),
        rounds: wbRounds,
        fieldSize: fieldSize,
        matchMap: p.matchMap,
        settings: settings,
        appEl: rootEl,
        matchUrls: urls,
        hoverData: hoverData,
        bracketView: isDE ? 'winners' : null,
        thirdPlaceMatches: (!isDE && settings.showThirdPlaceMatch) ? p.thirdPlaceMatches : [],
        isDE: isDE
      }));
    }

    // Render losers bracket
    if (showLB && lbRounds.length > 0) {
      rootEl.appendChild(renderBracketSection({
        title: _t('losersBracket', 'Losers Bracket (LB)'),
        subtitle: _t('sourceLabelsCrossBracket', 'Source labels on cross-bracket entries with jump navigation'),
        rounds: lbRounds,
        fieldSize: fieldSize,
        matchMap: p.matchMap,
        settings: settings,
        appEl: rootEl,
        matchUrls: urls,
        hoverData: hoverData,
        bracketView: 'losers',
        isDE: isDE
      }));
    }

    // Render extra finals — separate bracket-reset matches when enabled
    if (showFinals && p.finalMatches.length > 0) {
      var resetMatches = [];
      var otherFinals = [];
      for (i = 0; i < p.finalMatches.length; i++) {
        if (settings.useBracketReset && _ltIsGFResetMatch(p.finalMatches[i])) {
          resetMatches.push(p.finalMatches[i]);
        } else {
          otherFinals.push(p.finalMatches[i]);
        }
      }

      // Render each bracket-reset match in its own dedicated section
      for (i = 0; i < resetMatches.length; i++) {
        var resetSection = _ltRenderBracketResetSection(
          resetMatches[i], p.matchMap, settings, urls, rootEl, hoverData
        );
        resetSection.setAttribute('data-bracket-view', 'finals');
        rootEl.appendChild(resetSection);
      }

      // Render any remaining non-reset finals in the generic section
      if (otherFinals.length > 0) {
        var finalsSection = renderFinalsSection(otherFinals, p.matchMap, settings, urls, rootEl, hoverData);
        finalsSection.setAttribute('data-bracket-view', 'finals');
        rootEl.appendChild(finalsSection);
      }
    }

    // Render third-place matches (standalone — only if NOT rendered inline)
    if (showFinals && settings.showThirdPlaceMatch && p.thirdPlaceMatches.length > 0 && isDE) {
      rootEl.appendChild(renderThirdPlaceSection(
        p.thirdPlaceMatches, p.matchMap, settings, urls, rootEl, hoverData
      ));
    }

    bindHoverHighlight(rootEl, rootEl, scrollToMatchRef);
    _ltBindKeyboardNav(rootEl);

    if (pendingScrollRef) {
      var refToScroll = pendingScrollRef;
      pendingScrollRef = '';
      requestAnimationFrame(function() {
        _ltActivateAndScrollToMatchRef(rootEl, refToScroll);
      });
    }
  }

  function destroy() {
    _ltClearHighlights(rootEl);
    rootEl.innerHTML = '';
    rootEl.onmousemove = null;
    rootEl.onfocusin = null;
    rootEl.onmouseleave = null;
    rootEl.onfocusout = null;
    rootEl.onclick = null;
    rootEl.onkeydown = null;
    parsed = null;
  }

  function getMatchMap() {
    return ensureParsed().matchMap;
  }

  /**
   * Switch the bracket view mode and re-render with filtered rounds.
   *
   * Modes: 'full', 'top8', 'winners', 'losers'.
   * The bracket clears and re-renders so geometry, SVG connectors,
   * and layout are all recomputed for the visible rounds only.
   */
  function setViewMode(mode) {
    if (mode === viewMode) return;
    viewMode = mode;
    render();
    // Re-init view switcher for the recreated dropdown
    if (typeof initBracketViewSwitcher === 'function') {
      initBracketViewSwitcher();
    }
    // Re-init pan on recreated scroll wrappers
    rootEl.querySelectorAll('.lt-bracket-scroll').forEach(function(el) {
      if (typeof enableBracketPan === 'function') enableBracketPan(el);
    });
  }

  return {
    render: render,
    destroy: destroy,
    getMatchMap: getMatchMap,
    setViewMode: setViewMode
  };
}


/* ===================================================================
 *  Auto-init from data island
 * =================================================================== */

document.addEventListener('DOMContentLoaded', function() {
  var dataEl = document.getElementById('bracket-data');
  if (!dataEl) return;

  var data;
  try {
    data = JSON.parse(dataEl.textContent);
  } catch (e) {
    if (typeof console !== 'undefined' && console.error) {
      console.error('[lan_tournament_bracket] Failed to parse bracket data:', e);
    }
    return;
  }

  // Populate i18n strings from server-embedded translations
  if (data.strings) {
    _ltStrings = data.strings;
  }

  var rootEl = document.getElementById('bracket-app');
  if (!rootEl) return;

  var instance = createBracketInstance({
    rootId: 'bracket-app',
    data: data,
    matchUrls: data.match_urls || {}
  });

  if (instance) {
    instance.render();
    // Expose instance on the root element so applyMode() can delegate
    rootEl._ltBracketInstance = instance;
    // Re-init view switcher for dynamically created view-select dropdowns
    if (typeof initBracketViewSwitcher === 'function') {
      initBracketViewSwitcher();
    }
  }

  // Re-render on window resize (debounced)
  var resizeTimer;
  window.addEventListener('resize', function() {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(function() {
      if (instance) instance.render();
    }, 110);
  });
});
