"""
tests.unit.services.lan_tournament.test_downstream_impact
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The rows the correction panel lists for the matches a correction
reaches.

The panel used to print bracket shorthand and nothing else, so an
admin could not tell an untouched fixture apart from a confirmed
result about to be cleared without opening every one of them. These
pin what each row now claims.
"""

from datetime import datetime

import pytest
from flask import Flask
from flask_babel import Babel

from byceps.services.lan_tournament.lan_tournament_view_helpers import (
    build_downstream_impact,
    build_match_label,
)
from byceps.services.lan_tournament.models.bracket import Bracket
from byceps.services.lan_tournament.models.tournament import TournamentID
from byceps.services.lan_tournament.models.tournament_match import (
    CorrectionCase,
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

from tests.helpers import generate_uuid


TOURNAMENT_ID = TournamentID(generate_uuid())
NOW = datetime(2026, 9, 20, 12, 0)


@pytest.fixture(scope='module')
def app():
    a = Flask(__name__)
    a.config['TESTING'] = True
    Babel(a)
    return a


@pytest.fixture(autouse=True)
def _ctx(app):
    """The labels go through gettext, which needs an app context."""
    with app.app_context():
        yield


def _match(
    *,
    bracket=None,
    round_=None,
    match_order=None,
    confirmed_by=None,
) -> TournamentMatch:
    return TournamentMatch(
        id=TournamentMatchID(generate_uuid()),
        tournament_id=TOURNAMENT_ID,
        group_order=None,
        match_order=match_order,
        round=round_,
        next_match_id=None,
        confirmed_by=confirmed_by,
        created_at=NOW,
        bracket=bracket,
    )


def _contestant(match_id, score=None) -> TournamentMatchToContestant:
    return TournamentMatchToContestant(
        id=TournamentMatchToContestantID(generate_uuid()),
        tournament_match_id=match_id,
        team_id=None,
        participant_id=TournamentParticipantID(generate_uuid()),
        score=score,
        created_at=NOW,
    )


# ------------------------------------------------------------------ #
# labels
# ------------------------------------------------------------------ #


def test_label_spells_out_the_winners_bracket():
    """Rounds and matches are stored zero-based, shown one-based."""
    match = _match(bracket=Bracket.WINNERS, round_=1, match_order=0)

    assert build_match_label(match) == (
        'Winners bracket, round 2, match 1'
    )


def test_label_spells_out_the_losers_bracket():
    match = _match(bracket=Bracket.LOSERS, round_=2, match_order=1)

    assert build_match_label(match) == 'Losers bracket, round 3, match 2'


def test_label_of_a_grand_final_omits_the_round():
    """A grand final has no round worth naming; the game number does."""
    match = _match(bracket=Bracket.GRAND_FINAL, round_=0, match_order=1)

    assert build_match_label(match) == 'Grand final, match 2'


def test_label_of_a_third_place_match():
    match = _match(bracket=Bracket.THIRD_PLACE, round_=0, match_order=0)

    assert build_match_label(match) == 'Third-place match'


def test_label_without_a_bracket_falls_back_to_round_and_match():
    match = _match(round_=0, match_order=2)

    assert build_match_label(match) == 'Round 1, match 3'


def test_label_treats_missing_numbers_as_the_first():
    """Nulls must not surface as 'round None'."""
    match = _match(bracket=Bracket.WINNERS)

    assert build_match_label(match) == 'Winners bracket, round 1, match 1'


# ------------------------------------------------------------------ #
# state of each affected match
# ------------------------------------------------------------------ #


def test_confirmed_downstream_row_is_destructive():
    """A confirmed result is the thing the admin stands to lose."""
    match = _match(bracket=Bracket.WINNERS, confirmed_by=generate_uuid())
    contestants = [_contestant(match.id, 3), _contestant(match.id, 1)]

    (row,) = build_downstream_impact(
        [match],
        {match.id: contestants},
        CorrectionCase.CONFIRMED_DOWNSTREAM,
    )

    assert row.status == 'confirmed'
    assert row.status_label == 'Confirmed'
    assert row.impact_label == 'Result will be retracted'
    assert row.destructive
    assert row.open_slots == 0


def test_unplayed_downstream_row_only_loses_its_contestant():
    """Losing an advancement is routine and must not read as a loss."""
    match = _match(bracket=Bracket.LOSERS)
    contestants = [_contestant(match.id)]

    (row,) = build_downstream_impact(
        [match],
        {match.id: contestants},
        CorrectionCase.CONFIRMED_DOWNSTREAM,
    )

    assert row.status == 'pending'
    assert row.impact_label == 'Contestant will be removed'
    assert not row.destructive
    # The second slot is still open, and the panel says so.
    assert row.open_slots == 1


def test_scores_entered_but_unconfirmed_are_awaiting_confirmation():
    match = _match(bracket=Bracket.WINNERS)
    contestants = [_contestant(match.id, 2), _contestant(match.id, 0)]

    (row,) = build_downstream_impact(
        [match], {match.id: contestants}, CorrectionCase.UNCONFIRMED_DOWNSTREAM
    )

    assert row.status == 'reported'
    assert row.status_label == 'Awaiting confirmation'
    assert row.open_slots == 0


def test_single_contestant_confirmed_match_is_a_defwin():
    match = _match(bracket=Bracket.LOSERS, confirmed_by=generate_uuid())
    contestants = [_contestant(match.id)]

    (row,) = build_downstream_impact(
        [match], {match.id: contestants}, CorrectionCase.CONFIRMED_DOWNSTREAM
    )

    assert row.status == 'defwin'
    assert row.destructive
    assert row.open_slots == 0
    # A defwin has no score to clear; what it loses is the free
    # advancement, and the row says so rather than claiming a result.
    assert row.impact_label == 'Defwin will be retracted'


def test_bracket_reset_match_is_deleted_not_retracted():
    """Deletion is a different consequence and says so, confirmed or not."""
    match = _match(bracket=Bracket.GRAND_FINAL, match_order=1)

    (row,) = build_downstream_impact(
        [match], {match.id: []}, CorrectionCase.BRACKET_RESET_DELETION
    )

    assert row.impact_label == 'Will be deleted'
    assert row.destructive


def test_rows_keep_the_order_they_were_given():
    """The service returns the cascade in breadth-first order."""
    matches = [
        _match(bracket=Bracket.WINNERS, round_=1, match_order=0),
        _match(bracket=Bracket.LOSERS, round_=1, match_order=1),
        _match(bracket=Bracket.LOSERS, round_=2, match_order=1),
    ]

    rows = build_downstream_impact(
        matches, {}, CorrectionCase.UNCONFIRMED_DOWNSTREAM
    )

    assert [row.match.id for row in rows] == [m.id for m in matches]
    assert [row.label for row in rows] == [
        'Winners bracket, round 2, match 1',
        'Losers bracket, round 2, match 2',
        'Losers bracket, round 3, match 2',
    ]


def test_missing_contestants_do_not_break_a_row():
    """A match whose contestants were not fetched still renders."""
    match = _match(bracket=Bracket.WINNERS)

    (row,) = build_downstream_impact(
        [match], {}, CorrectionCase.UNCONFIRMED_DOWNSTREAM
    )

    assert row.contestants == []
    assert row.open_slots == 2
