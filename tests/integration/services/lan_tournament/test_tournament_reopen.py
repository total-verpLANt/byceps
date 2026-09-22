"""
tests.integration.services.lan_tournament.test_tournament_reopen

COMPLETED is recoverable: reopening returns the tournament to
ONGOING and clears the recorded winner.
"""

import pytest

from byceps.services.lan_tournament import (
    tournament_log_service,
    tournament_participant_service,
    tournament_repository,
    tournament_service,
)
from byceps.services.lan_tournament.models import (
    ContestantType,
    EliminationMode,
    GameFormat,
    TournamentStatus,
)
from byceps.services.party.models import PartyID
from byceps.services.ticketing import ticket_creation_service


PARTY_ID = PartyID('lan-party-2026-reopen')


@pytest.fixture(scope='module')
def party(make_party, brand):
    return make_party(brand, PARTY_ID, 'LAN Party 2026 Reopen')


@pytest.fixture(scope='module')
def ticket_category(make_ticket_category, party):
    return make_ticket_category(party.id, 'Reopen Entry')


@pytest.fixture(scope='module')
def ticketed(make_user, ticket_category):
    users = [make_user(f'Reopen{i:02d}') for i in range(4)]
    for user in users:
        ticket_creation_service.create_ticket(ticket_category, user, user=user)
    return users


@pytest.fixture(scope='module')
def admin(make_user):
    return make_user('ReopenAdmin')


def _completed_round_robin(name, ticketed):
    """A round robin, i.e. the mode with no deciding match at all."""
    result = tournament_service.create_tournament(
        PARTY_ID,
        name,
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.ROUND_ROBIN,
        contestant_type=ContestantType.SOLO,
        max_players=8,
    )
    assert result.is_ok(), result.unwrap_err()
    tournament, _ = result.unwrap()

    assert tournament_service.change_status(
        tournament.id, TournamentStatus.REGISTRATION_OPEN
    ).is_ok()
    for user in ticketed:
        assert tournament_participant_service.join_tournament(
            tournament.id, user.id
        ).is_ok()
    assert tournament_service.change_status(
        tournament.id, TournamentStatus.REGISTRATION_CLOSED
    ).is_ok()
    return tournament


def test_a_completed_round_robin_can_be_reopened(party, ticketed, admin):
    """The case that used to be a dead end.

    ROUND_ROBIN never satisfies is_deciding_match(), so the
    retraction cascade's revert could not reach it either.
    """
    tournament = _completed_round_robin('Reopen round robin', ticketed)
    from byceps.services.lan_tournament import tournament_match_service

    assert tournament_match_service.generate_round_robin_bracket(
        tournament.id, initiator_id=admin.id
    ).is_ok()
    assert tournament_service.change_status(
        tournament.id, TournamentStatus.ONGOING, admin.id
    ).is_ok()
    assert tournament_service.change_status(
        tournament.id, TournamentStatus.COMPLETED, admin.id
    ).is_ok()
    assert (
        tournament_service.get_tournament(tournament.id).tournament_status
        == TournamentStatus.COMPLETED
    )

    result = tournament_service.change_status(
        tournament.id, TournamentStatus.ONGOING, admin.id
    )

    assert result.is_ok(), result.unwrap_err()
    reopened = tournament_service.get_tournament(tournament.id)
    assert reopened.tournament_status == TournamentStatus.ONGOING


def test_reopening_clears_the_recorded_winner(party, ticketed, admin):
    tournament = _completed_round_robin('Reopen clears winner', ticketed)
    from byceps.services.lan_tournament import tournament_match_service

    assert tournament_match_service.generate_round_robin_bracket(
        tournament.id, initiator_id=admin.id
    ).is_ok()
    assert tournament_service.change_status(
        tournament.id, TournamentStatus.ONGOING, admin.id
    ).is_ok()
    participants = (
        tournament_participant_service.get_participants_for_tournament(
            tournament.id
        )
    )
    assert tournament_repository.set_tournament_winner(
        tournament.id,
        winner_team_id=None,
        winner_participant_id=participants[0].id,
    ).is_ok()
    tournament_repository.commit_session()
    assert tournament_service.change_status(
        tournament.id, TournamentStatus.COMPLETED, admin.id
    ).is_ok()
    assert (
        tournament_service.get_tournament(tournament.id).winner_participant_id
        is not None
    )

    result = tournament_service.change_status(
        tournament.id, TournamentStatus.ONGOING, admin.id
    )

    assert result.is_ok(), result.unwrap_err()
    reopened = tournament_service.get_tournament(tournament.id)
    assert reopened.winner_participant_id is None
    assert reopened.winner_team_id is None
    # The returned dataclass agrees with what was persisted.
    assert result.unwrap()[0].winner_participant_id is None


def test_reopening_is_logged_with_its_initiator(party, ticketed, admin):
    tournament = _completed_round_robin('Reopen is logged', ticketed)
    from byceps.services.lan_tournament import tournament_match_service

    assert tournament_match_service.generate_round_robin_bracket(
        tournament.id, initiator_id=admin.id
    ).is_ok()
    for status in (TournamentStatus.ONGOING, TournamentStatus.COMPLETED):
        assert tournament_service.change_status(
            tournament.id, status, admin.id
        ).is_ok()

    assert tournament_service.change_status(
        tournament.id, TournamentStatus.ONGOING, admin.id
    ).is_ok()

    entries = [
        entry
        for entry in tournament_log_service.get_entries_for_tournament(
            tournament.id
        )
        if entry.event_type == 'tournament-status-changed'
        and entry.data['old_status'] == TournamentStatus.COMPLETED.name
    ]
    assert len(entries) == 1
    assert entries[0].initiator_id == admin.id
    assert entries[0].data['new_status'] == TournamentStatus.ONGOING.name


def test_completed_is_still_not_cancellable(party, ticketed, admin):
    """Only ONGOING was opened up; the rest of the dead end stands."""
    tournament = _completed_round_robin('Reopen not cancellable', ticketed)
    from byceps.services.lan_tournament import tournament_match_service

    assert tournament_match_service.generate_round_robin_bracket(
        tournament.id, initiator_id=admin.id
    ).is_ok()
    for status in (TournamentStatus.ONGOING, TournamentStatus.COMPLETED):
        assert tournament_service.change_status(
            tournament.id, status, admin.id
        ).is_ok()

    result = tournament_service.change_status(
        tournament.id, TournamentStatus.CANCELLED, admin.id
    )

    assert result.is_err()
    assert (
        tournament_service.get_tournament(tournament.id).tournament_status
        == TournamentStatus.COMPLETED
    )


def test_reopening_skips_the_pre_start_bracket_validation(
    party, ticketed, admin, monkeypatch
):
    """A reopen is not a start.

    validate_bracket_for_start() asserts the PRE-start topology, but
    by the time a tournament is completed, play has rewired the
    bracket -- a DE bracket reset wires GF M1.next_match_id, and
    confirmations fill contestant rows. Running that check on the way
    back to ONGOING would refuse the reopen outright, which would
    defeat the point of having one.

    Driven by a stubbed validator rather than a deliberately
    corrupted bracket: what is under test is which transitions call
    it, not what it reports.
    """
    from byceps.services.lan_tournament import tournament_match_service

    tournament = _completed_round_robin('Reopen skips validation', ticketed)
    assert tournament_match_service.generate_round_robin_bracket(
        tournament.id, initiator_id=admin.id
    ).is_ok()
    for status in (TournamentStatus.ONGOING, TournamentStatus.COMPLETED):
        assert tournament_service.change_status(
            tournament.id, status, admin.id
        ).is_ok()

    calls = []

    def _always_violates(tournament_id, **kwargs):
        calls.append(tournament_id)
        return ['stubbed structural violation']

    monkeypatch.setattr(
        tournament_service.tournament_match_service,
        'validate_bracket_for_start',
        _always_violates,
    )

    result = tournament_service.change_status(
        tournament.id, TournamentStatus.ONGOING, admin.id
    )

    assert result.is_ok(), result.unwrap_err()
    assert calls == [], 'the reopen must not run the pre-start validation'
    assert (
        tournament_service.get_tournament(tournament.id).tournament_status
        == TournamentStatus.ONGOING
    )


def test_a_real_start_still_runs_the_pre_start_bracket_validation(
    party, ticketed, admin, monkeypatch
):
    """The counterpart: narrowing `is_start` must not have relaxed it."""
    from byceps.services.lan_tournament import tournament_match_service

    tournament = _completed_round_robin('Start still validates', ticketed)
    assert tournament_match_service.generate_round_robin_bracket(
        tournament.id, initiator_id=admin.id
    ).is_ok()

    def _always_violates(tournament_id, **kwargs):
        return ['stubbed structural violation']

    monkeypatch.setattr(
        tournament_service.tournament_match_service,
        'validate_bracket_for_start',
        _always_violates,
    )

    result = tournament_service.change_status(
        tournament.id, TournamentStatus.ONGOING, admin.id
    )

    assert result.is_err()
    assert 'stubbed structural violation' in result.unwrap_err()
    assert (
        tournament_service.get_tournament(tournament.id).tournament_status
        == TournamentStatus.REGISTRATION_CLOSED
    )
