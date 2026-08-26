"""
tests.unit.services.lan_tournament.test_fc3_in_place_correction_plan
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Decide when a correction is written in place instead of retracted.
"""

from dataclasses import replace
from datetime import datetime, UTC
from unittest.mock import patch

import pytest

from byceps.services.lan_tournament import tournament_match_service as engine
from byceps.services.lan_tournament.models.tournament_match import (
    TournamentMatch,
    TournamentMatchID,
)
from byceps.services.lan_tournament.models.tournament_match_to_contestant import (
    TournamentMatchToContestant,
    TournamentMatchToContestantID,
)
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipantID,
)
from byceps.util.result import Err, Ok

from tests.helpers import generate_uuid


_S = 'byceps.services.lan_tournament.tournament_match_service'

MATCH_ID = TournamentMatchID(generate_uuid())
NEXT_ID = TournamentMatchID(generate_uuid())
LOSER_NEXT_ID = TournamentMatchID(generate_uuid())
USER_ID = generate_uuid()
PARTICIPANT_A = TournamentParticipantID(generate_uuid())
PARTICIPANT_B = TournamentParticipantID(generate_uuid())


def _match(**overrides) -> TournamentMatch:
    fields = dict(
        id=MATCH_ID,
        tournament_id=generate_uuid(),
        group_order=None,
        match_order=0,
        round=0,
        next_match_id=NEXT_ID,
        loser_next_match_id=None,
        confirmed_by=USER_ID,
        created_at=datetime.now(UTC),
    )
    fields.update(overrides)
    return TournamentMatch(**fields)


def _contestant(participant_id, score) -> TournamentMatchToContestant:
    return TournamentMatchToContestant(
        id=TournamentMatchToContestantID(generate_uuid()),
        tournament_match_id=MATCH_ID,
        team_id=None,
        participant_id=participant_id,
        score=score,
        created_at=datetime.now(UTC),
    )


def _plan(match, contestants, proposed, *, validation=None):
    """Plan with `proposed` scores, given in contestant order."""
    id_to_score = {c.id: s for c, s in zip(contestants, proposed, strict=True)}
    corrected = {
        c.participant_id: s for c, s in zip(contestants, proposed, strict=True)
    }
    with (
        patch(f'{_S}.tournament_repository') as mock_repo,
        patch(f'{_S}._validate_match_scores') as mock_validate,
    ):
        mock_repo.find_match_fresh.return_value = match
        mock_validate.return_value = (
            Ok(id_to_score) if validation is None else validation
        )
        return engine._plan_in_place_correction(
            MATCH_ID, contestants, corrected
        )


def _pair():
    return [_contestant(PARTICIPANT_A, 3), _contestant(PARTICIPANT_B, 1)]


# fmt: off
@pytest.mark.parametrize(
    'overrides',
    [
        {'next_match_id': NEXT_ID},
        {'next_match_id': None, 'loser_next_match_id': LOSER_NEXT_ID},
        {'next_match_id': NEXT_ID, 'loser_next_match_id': LOSER_NEXT_ID},
    ],
)
# fmt: on
def test_same_winner_on_an_advancing_match_is_planned(overrides):
    contestants = _pair()

    plan = _plan(_match(**overrides), contestants, (5, 2))

    assert plan is not None
    assert plan.winner.id == contestants[0].id
    assert plan.id_to_score == {contestants[0].id: 5, contestants[1].id: 2}


def test_same_winner_with_the_scores_swapped_between_sides_is_planned():
    contestants = [_contestant(PARTICIPANT_A, 1), _contestant(PARTICIPANT_B, 3)]

    plan = _plan(_match(), contestants, (0, 9))

    assert plan is not None
    assert plan.winner.id == contestants[1].id


# fmt: off
@pytest.mark.parametrize(
    ('proposed', 'reason'),
    [
        ((1, 3), 'the winner changes'),
        ((2, 2), 'the result becomes a draw'),
        ((3, 1), 'the scores are unchanged'),
    ],
)
# fmt: on
def test_anything_but_a_changed_margin_takes_the_full_path(proposed, reason):
    assert _plan(_match(), _pair(), proposed) is None, reason


def test_a_drawn_result_is_never_planned():
    contestants = [_contestant(PARTICIPANT_A, 2), _contestant(PARTICIPANT_B, 2)]

    assert _plan(_match(), contestants, (3, 1)) is None


# fmt: off
@pytest.mark.parametrize(
    'overrides',
    [
        {'next_match_id': None, 'loser_next_match_id': None},
        {'confirmed_by': None},
    ],
)
# fmt: on
def test_a_match_that_does_not_advance_or_is_unconfirmed_takes_the_full_path(
    overrides,
):
    assert _plan(_match(**overrides), _pair(), (5, 2)) is None


def test_a_vanished_match_takes_the_full_path():
    assert _plan(None, _pair(), (5, 2)) is None


def test_rejected_scores_take_the_full_path():
    plan = _plan(
        _match(), _pair(), (5, 2), validation=Err('Score cannot be negative.')
    )

    assert plan is None


def test_a_match_without_exactly_two_contestants_takes_the_full_path():
    contestants = _pair()
    third = _contestant(TournamentParticipantID(generate_uuid()), 0)

    assert _plan(_match(), contestants[:1], (5,)) is None
    assert _plan(_match(), [*contestants, third], (5, 2, 0)) is None


def test_a_defwin_slot_takes_the_full_path():
    contestants = [
        _contestant(PARTICIPANT_A, 3),
        replace(_contestant(None, 1), participant_id=None, team_id=None),
    ]
    id_to_score = {c.id: s for c, s in zip(contestants, (5, 2), strict=True)}
    with (
        patch(f'{_S}.tournament_repository') as mock_repo,
        patch(f'{_S}._validate_match_scores', return_value=Ok(id_to_score)),
    ):
        mock_repo.find_match_fresh.return_value = _match()

        plan = engine._plan_in_place_correction(
            MATCH_ID, contestants, {PARTICIPANT_A: 5}
        )

    assert plan is None
