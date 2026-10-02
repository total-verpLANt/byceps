"""
tests.integration.services.lan_tournament.test_ffa_removed_contestants
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

A contestant removed after their FFA lobby was confirmed keeps the
entry, but takes no slot at the cut and can never win.
"""

from datetime import datetime, UTC
from itertools import count

import pytest

from byceps.database import db
from byceps.services.lan_tournament import (
    tournament_match_service,
    tournament_participant_service,
    tournament_qualification_service,
    tournament_repository,
    tournament_score_service,
    tournament_seeding_service,
    tournament_service,
)
from byceps.services.lan_tournament.models import ContestantType
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.playoff import PlayoffReleaseMode
from byceps.services.lan_tournament.models.score_ordering import ScoreOrdering
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipant,
    TournamentParticipantID,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.party.models import PartyID
from byceps.util.uuid import generate_uuid7


PARTY_ID = PartyID('lan-party-ffa-removed-contestants')

_counter = count(1)

KINDS = ['plain_ffa', 'highscore_ffa']
CLEAN_TABLE = [4, 3, 2, 1]
TIED_TABLE = [3, 2, 1, 1]


@pytest.fixture(scope='module')
def party(make_party, make_brand):
    brand = make_brand('ffaremovedcontestantsbrand', 'FFA Removed Brand')
    return make_party(brand, PARTY_ID, 'LAN Party FFA Removed Contestants')


@pytest.fixture(scope='module')
def players(make_user):
    return [make_user(f'FfaRemovedPlayer{i}') for i in range(8)]


@pytest.fixture(scope='module')
def admin(make_user):
    return make_user('FfaRemovedAdmin')


@pytest.fixture
def make_ffa(party, players, admin):
    """Return a started FFA tournament with both lobbies of round 0 played."""
    created = []

    def _make(kind, point_table, cut=2):
        common = {
            'contestant_type': ContestantType.SOLO,
            'point_table': point_table,
            'group_size_min': 4,
            'group_size_max': 6,
            'advancement_count': cut,
        }
        if kind == 'plain_ffa':
            args = {
                **common,
                'game_format': GameFormat.FREE_FOR_ALL,
                'elimination_mode': EliminationMode.SINGLE_ELIMINATION,
                'tournament_status': TournamentStatus.REGISTRATION_CLOSED,
                'max_players': 8,
            }
        else:
            args = {
                **common,
                'game_format': GameFormat.HIGHSCORE,
                'elimination_mode': EliminationMode.NONE,
                'score_ordering': ScoreOrdering.HIGHER_IS_BETTER,
                'tournament_status': TournamentStatus.ONGOING,
                'playoff_game_format': GameFormat.FREE_FOR_ALL,
                'playoff_elimination_mode': EliminationMode.SINGLE_ELIMINATION,
                'playoff_qualifier_count': 8,
                'playoff_release_mode': PlayoffReleaseMode.AUTOMATIC,
            }
        tournament, _ = tournament_service.create_tournament(
            PARTY_ID, f'FFA Removed {next(_counter)}', **args
        ).unwrap()
        created.append(tournament)
        for user in players:
            tournament_repository.create_participant(
                TournamentParticipant(
                    id=TournamentParticipantID(generate_uuid7()),
                    user_id=user.id,
                    tournament_id=tournament.id,
                    substitute_player=False,
                    team_id=None,
                    created_at=datetime.now(UTC),
                )
            )
        db.session.commit()
        if kind == 'plain_ffa':
            board = tournament_seeding_service.get_board(
                tournament.id, initiator_id=admin.id
            ).unwrap()
            tournament_seeding_service.generate_from_seeding(
                tournament.id,
                expected_version=board.version,
                initiator_id=admin.id,
            ).unwrap()
            tournament_service.change_status(
                tournament.id, TournamentStatus.ONGOING, admin.id
            ).unwrap()
        else:
            participants = (
                tournament_repository.get_participants_for_tournament(
                    tournament.id
                )
            )
            for value, participant in enumerate(participants, start=1):
                tournament_score_service.submit_score(
                    tournament.id, value * 10, participant_id=participant.id
                ).unwrap()
            tournament_score_service.close_leaderboard(
                tournament.id, initiator_id=admin.id
            ).unwrap()
        lobbies = _lobbies(tournament)
        assert len(lobbies) == 2
        for lobby in lobbies:
            _play(lobby, admin)
        return tournament

    yield _make
    db.session.rollback()
    for tournament in created:
        if tournament_repository.find_tournament(tournament.id) is not None:
            tournament_service.delete_tournament(tournament.id)


def _lobbies(tournament):
    return sorted(
        (
            m
            for m in tournament_repository.get_matches_for_tournament(
                tournament.id
            )
            if m.round == (1 if tournament.playoff_game_format else 0)
            or m.phase == 2
        ),
        key=lambda m: m.group_order or 0,
    )


def _members(match):
    """Return the lobby's contestants by recorded placement, then ID."""
    contestants = tournament_match_service.get_contestants_for_match(match.id)
    return [
        str(c.participant_id)
        for c in sorted(
            contestants,
            key=lambda c: (
                c.placement is None,
                c.placement or 0,
                str(c.participant_id),
            ),
        )
    ]


def _play(match, admin):
    """Place the lobby in its natural order and confirm it."""
    placed = tournament_match_service.set_ffa_placements(
        match.id, {cid: i + 1 for i, cid in enumerate(_members(match))}
    )
    assert placed.is_ok(), placed.unwrap_err()
    confirmed = tournament_match_service.confirm_ffa_match(match.id, admin.id)
    assert confirmed.is_ok(), confirmed.unwrap_err()


def _remove(tournament, participant_id, admin):
    removed = tournament_participant_service.admin_remove_participant(
        tournament.id,
        TournamentParticipantID(participant_id),
        initiator=admin,
    )
    assert removed.is_ok(), removed.unwrap_err()


@pytest.mark.parametrize('kind', KINDS)
def test_removed_lobby_winner_takes_no_slot_in_the_next_round(
    make_ffa, admin, kind
):
    tournament = make_ffa(kind, CLEAN_TABLE)
    first, second = (_members(m) for m in _lobbies(tournament))
    victim = first[0]
    _remove(tournament, victim, admin)

    target = tournament_seeding_service.prepare_ffa_round_draft(
        tournament.id, initiator_id=admin.id
    ).unwrap()
    board = tournament_seeding_service.get_board(
        tournament.id, target, initiator_id=admin.id
    ).unwrap()

    assert victim not in board.state.roster
    assert set(board.state.roster) == set(first[1:3]) | set(second[:2])


@pytest.mark.parametrize('kind', KINDS)
def test_tournament_never_completes_with_a_removed_winner(
    make_ffa, admin, kind
):
    tournament = make_ffa(kind, CLEAN_TABLE)
    first_lobby = _lobbies(tournament)[0]
    first = _members(first_lobby)
    victim = first[0]
    _remove(tournament, victim, admin)
    target = tournament_seeding_service.prepare_ffa_round_draft(
        tournament.id, initiator_id=admin.id
    ).unwrap()
    board = tournament_seeding_service.get_board(
        tournament.id, target, initiator_id=admin.id
    ).unwrap()
    tournament_seeding_service.generate_from_seeding(
        tournament.id,
        target,
        expected_version=board.version,
        initiator_id=admin.id,
    ).unwrap()

    (final,) = [
        m
        for m in tournament_repository.get_matches_for_tournament(tournament.id)
        if (m.round or 0) > first_lobby.round
    ]
    assert victim not in _members(final)
    _play(final, admin)

    found = tournament_repository.get_tournament(tournament.id)
    active = {
        str(p.id)
        for p in tournament_repository.get_participants_for_tournament(
            tournament.id
        )
    }
    assert found.tournament_status is TournamentStatus.COMPLETED
    assert str(found.winner_participant_id) != victim
    assert str(found.winner_participant_id) in active


@pytest.mark.parametrize('kind', KINDS)
def test_a_cut_tie_of_the_removed_contestant_disappears(make_ffa, admin, kind):
    tournament = make_ffa(kind, TIED_TABLE, cut=3)
    # Places 3 and 4 tie across the cut of 3; one of them leaves.
    first_lobby, second_lobby = _lobbies(tournament)
    members = _members(first_lobby)
    before = tournament_qualification_service.get_ffa_cut_ties(tournament.id)
    assert {t.lobby for t in before} == {
        first_lobby.group_order or 0,
        second_lobby.group_order or 0,
    }

    _remove(tournament, members[2], admin)

    after = tournament_qualification_service.get_ffa_cut_ties(tournament.id)
    assert [t.lobby for t in after] == [second_lobby.group_order or 0]
    for tie in after:
        assert members[2] not in {c.contestant_id for c in tie.contestants}
