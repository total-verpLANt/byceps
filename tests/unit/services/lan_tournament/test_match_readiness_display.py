"""Tests for the shared §25.3 readiness display derivation
(`models/match_readiness.py`) used by both view layers.
"""

from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from byceps.services.lan_tournament.models.match_readiness import (
    ContestantIdentity,
    MatchPairing,
    READINESS_FILTER_BUCKETS,
    ReadinessDisplayStatus,
    derive_match_readiness,
    real_contestants,
    side_for_contestant,
)
from byceps.services.lan_tournament.models.tournament import TournamentID
from byceps.services.lan_tournament.models.tournament_match import (
    MatchPairingID,
    MatchSide,
    TournamentMatch,
    TournamentMatchID,
)
from byceps.services.lan_tournament.models.tournament_match_to_contestant import (
    TournamentMatchToContestant,
    TournamentMatchToContestantID,
)

NOW = datetime.now(UTC)


def _make_match(**kwargs) -> TournamentMatch:
    defaults = dict(
        id=TournamentMatchID(uuid4()),
        tournament_id=TournamentID(uuid4()),
        group_order=None,
        match_order=1,
        round=1,
        next_match_id=None,
        confirmed_by=None,
        created_at=NOW,
    )
    defaults.update(kwargs)
    return TournamentMatch(**defaults)


def _make_contestant(*, participant_id=None, team_id=None, match_id=None):
    return TournamentMatchToContestant(
        id=TournamentMatchToContestantID(uuid4()),
        tournament_match_id=match_id or TournamentMatchID(uuid4()),
        team_id=team_id,
        participant_id=participant_id,
        score=None,
        created_at=NOW,
    )


def _two_sided():
    match = _make_match()
    a = _make_contestant(participant_id=uuid4(), match_id=match.id)
    b = _make_contestant(participant_id=uuid4(), match_id=match.id)
    contestants = [a, b]
    return match, contestants


def _pair(match, contestants):
    pairing = MatchPairing(
        id=MatchPairingID(uuid4()), match_id=match.id,
        tournament_id=match.tournament_id, generation=3,
        side_a=ContestantIdentity(kind='participant', id=contestants[0].participant_id),
        side_b=ContestantIdentity(kind='participant', id=contestants[1].participant_id),
        started_at=NOW,
    )
    return replace(match, pairing_id=pairing.id, pairing_generation=3), pairing


# fmt: off
@pytest.mark.parametrize(
    (
        'with_defwin',
        'ready_at_a',
        'ready_at_b',
        'expected_status',
        'expected_ready_sides',
    ),
    [
        (False, None,     None,     ReadinessDisplayStatus.OPEN,              ()),
        (False, NOW,      None,     ReadinessDisplayStatus.PARTIALLY_READY,   (MatchSide.A,)),
        (False, None,     NOW,      ReadinessDisplayStatus.PARTIALLY_READY,   (MatchSide.B,)),
        (False, NOW,      NOW,      ReadinessDisplayStatus.BOTH_READY,        (MatchSide.A, MatchSide.B)),
        (True,  None,     None,     ReadinessDisplayStatus.NOT_YET_OCCUPIED,  ()),
        (True,  NOW,      NOW,      ReadinessDisplayStatus.NOT_YET_OCCUPIED,  ()),
    ],
)
# fmt: on
def test_derive_match_readiness(
    with_defwin, ready_at_a, ready_at_b, expected_status, expected_ready_sides
):
    match = _make_match(ready_at_a=ready_at_a, ready_at_b=ready_at_b)
    contestants = [_make_contestant(participant_id=uuid4(), match_id=match.id)]
    if not with_defwin:
        contestants.append(_make_contestant(participant_id=uuid4(), match_id=match.id))
    else:
        # DEFWIN placeholder slot: no participant and no team assigned.
        contestants.append(_make_contestant())

    pairing = None
    if not with_defwin:
        match, pairing = _pair(match, contestants)
    readiness = derive_match_readiness(
        match, contestants, pairing=pairing, supports_readiness=True,
    )

    assert readiness.status is expected_status
    assert readiness.ready_sides == expected_ready_sides


def test_real_contestants_excludes_defwin_slots():
    a = _make_contestant(participant_id=uuid4())
    defwin = _make_contestant()
    b = _make_contestant(participant_id=uuid4())

    real = real_contestants([a, defwin, b])

    assert [c.id for c in real] == [a.id, b.id]


def test_side_for_contestant_maps_sides():
    a = _make_contestant(participant_id=uuid4())
    b = _make_contestant(participant_id=uuid4())

    assert side_for_contestant([a, b], a.id) is MatchSide.A
    assert side_for_contestant([a, b], b.id) is MatchSide.B
    assert side_for_contestant([a, b], uuid4()) is None


def test_confirmed_match_still_derives_both_ready():
    """Derivation is independent of confirmation state; controls are
    hidden in the view layer instead."""
    match, contestants = _two_sided()
    match, pairing = _pair(match, contestants)
    match = replace(
        match,
        confirmed_by=uuid4(),
        ready_at_a=NOW,
        ready_at_b=NOW,
    )

    readiness = derive_match_readiness(
        match, contestants, pairing=pairing, supports_readiness=True,
    )

    assert readiness.status is ReadinessDisplayStatus.BOTH_READY
    assert readiness.display_status == 'confirmed'
    assert not readiness.mutation_available


# fmt: off
@pytest.mark.parametrize('invalid', [
    'missing', 'pointer', 'generation', 'match', 'tournament', 'ended',
    'replacement', 'opposite', 'kind', 'duplicate', 'unknown', 'foreign_row',
])
# fmt: on
def test_projection_uses_current_pairing(invalid):
    match, contestants = _two_sided()
    match, pairing = _pair(match, contestants)
    match = replace(match, ready_at_a=NOW, ready_at_b=NOW, ready_by_a=uuid4())
    if invalid == 'missing':
        pairing = None
    elif invalid == 'pointer':
        pairing = replace(pairing, id=uuid4())
    elif invalid == 'generation':
        pairing = replace(pairing, generation=2)
    elif invalid == 'match':
        pairing = replace(pairing, match_id=uuid4())
    elif invalid == 'tournament':
        pairing = replace(pairing, tournament_id=uuid4())
    elif invalid == 'ended':
        pairing = replace(pairing, ended_at=NOW)
    elif invalid == 'replacement':
        pairing = replace(pairing, side_b=ContestantIdentity(kind='participant', id=uuid4()))
    elif invalid == 'opposite':
        pairing = replace(pairing, id=uuid4(), side_a=pairing.side_b, side_b=pairing.side_a)
    elif invalid == 'kind':
        pairing = replace(pairing, side_a=replace(pairing.side_a, kind='team'))
    elif invalid == 'duplicate':
        pairing = replace(pairing, side_b=pairing.side_a)
    elif invalid == 'unknown':
        pairing = replace(pairing, side_a=replace(pairing.side_a, kind='unknown'))
    elif invalid == 'foreign_row':
        contestants[0] = replace(contestants[0], tournament_match_id=uuid4())
    projection = derive_match_readiness(
        match, contestants, pairing=pairing, supports_readiness=True,
    )
    assert projection.ready_sides == ()
    assert projection.ready_at_a is projection.ready_at_b is projection.ready_by_a is None
    assert not projection.pairing_valid and not projection.mutation_available


def test_stable_pairing_side_survives_opposite_row_order():
    match, contestants = _two_sided()
    match, pairing = _pair(match, contestants)
    actor = uuid4()
    match = replace(match, ready_at_a=NOW, ready_by_a=actor)
    projection = derive_match_readiness(
        match, list(reversed(contestants)), pairing=pairing, supports_readiness=True,
    )
    assert projection.ready_sides == (MatchSide.A,)
    assert projection.ready_by_a == actor
    assert side_for_contestant(list(reversed(contestants)), contestants[0].id, pairing=pairing) is MatchSide.A
    assert side_for_contestant(list(reversed(contestants)), contestants[1].id, pairing=pairing) is MatchSide.B


# fmt: off
@pytest.mark.parametrize('count', [0, 1])
# fmt: on
def test_waiting_distinguishes_zero_and_one_assigned(count):
    match, contestants = _two_sided()
    projection = derive_match_readiness(
        replace(match, ready_at_a=NOW), contestants[:count],
        pairing=None, supports_readiness=True,
    )
    assert projection.status is ReadinessDisplayStatus.NOT_YET_OCCUPIED
    assert projection.assigned_contestant_count == count
    assert not projection.assignment_complete
    assert not projection.mutation_available
    assert projection.ready_sides == ()


# fmt: off
@pytest.mark.parametrize('supports,real,placeholders,claims,confirmed,expected', [
    (True,  2, 0, 0, False, 'not_ready'),
    (True,  2, 0, 1, False, 'partially_ready'),
    (True,  2, 0, 2, False, 'both_ready'),
    (True,  1, 0, 0, False, 'waiting'),
    (True,  0, 0, 0, False, 'waiting'),
    (True,  1, 1, 2, False, 'waiting'),
    (True,  0, 2, 0, False, 'waiting'),
    (True,  2, 0, 2, True,  'finished'),
    (True,  1, 1, 0, True,  'finished'),
    (True,  0, 2, 0, True,  'finished'),
    (False, 3, 0, 0, False, 'no_readiness'),
    (False, 2, 0, 2, False, 'no_readiness'),
    (False, 1, 0, 0, False, 'waiting'),
    (False, 1, 1, 0, False, 'waiting'),
    (False, 0, 0, 0, False, 'waiting'),
    (False, 3, 0, 0, True,  'finished'),
])
# fmt: on
def test_filter_bucket_table(supports, real, placeholders, claims, confirmed, expected):
    match = _make_match(
        ready_at_a=NOW if claims >= 1 else None,
        ready_at_b=NOW if claims == 2 else None,
        confirmed_by=uuid4() if confirmed else None,
    )
    contestants = [
        _make_contestant(participant_id=uuid4(), match_id=match.id)
        for _ in range(real)
    ] + [_make_contestant(match_id=match.id) for _ in range(placeholders)]
    pairing = None
    if supports and real == 2:
        match, pairing = _pair(match, contestants)
    readiness = derive_match_readiness(
        match, contestants, pairing=pairing, supports_readiness=supports,
    )
    assert readiness.filter_bucket == expected
    assert readiness.filter_bucket in READINESS_FILTER_BUCKETS
