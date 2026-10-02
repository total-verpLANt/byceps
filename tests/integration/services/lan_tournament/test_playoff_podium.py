"""
tests.integration.services.lan_tournament.test_playoff_podium
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The podium of a playoff tournament comes from phase 2 alone. The
RR to SE and highscore to FFA flows are pinned in `test_playoff_flows`.
"""

from datetime import datetime, UTC
from itertools import count
from uuid import UUID

import pytest

from byceps.database import db
from byceps.services.lan_tournament import (
    tournament_match_service,
    tournament_qualification_service,
    tournament_repository,
    tournament_seeding_service,
    tournament_service,
)
from byceps.services.lan_tournament.models import ContestantType
from byceps.services.lan_tournament.models.bracket import Bracket
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.playoff import PlayoffReleaseMode
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipant,
    TournamentParticipantID,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.party.models import PartyID
from byceps.util.uuid import generate_uuid7


PARTY_ID = PartyID('lan-party-2024-playoff-podium')

_counter = count(1)


@pytest.fixture(scope='module')
def party(make_party, make_brand):
    brand = make_brand('playoffpodiumbrand', 'Playoff Podium Brand')
    return make_party(brand, PARTY_ID, 'LAN Party 2024 Playoff Podium')


@pytest.fixture(scope='module')
def users(make_user):
    return [make_user(f'PlayoffPodiumUser{i}') for i in range(8)]


@pytest.fixture
def tournament(party, users):
    result = tournament_service.create_tournament(
        PARTY_ID,
        f'Playoff Podium Tournament {next(_counter)}',
        contestant_type=ContestantType.SOLO,
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.ROUND_ROBIN,
        tournament_status=TournamentStatus.REGISTRATION_CLOSED,
        playoff_game_format=GameFormat.ONE_V_ONE,
        playoff_elimination_mode=EliminationMode.DOUBLE_ELIMINATION,
        playoff_group_count=2,
        playoff_qualifiers_per_group=2,
        playoff_release_mode=PlayoffReleaseMode.MANUAL,
    )
    assert result.is_ok(), result.unwrap_err()
    tournament, _ = result.unwrap()
    for user in users:
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
    yield tournament
    db.session.rollback()
    if tournament_repository.find_tournament(tournament.id) is not None:
        tournament_service.delete_tournament(tournament.id)


def _matches(tournament, phase):
    return [
        m
        for m in tournament_repository.get_matches_for_tournament(tournament.id)
        if m.phase == phase
    ]


def _contestants(match):
    return [
        str(c.participant_id)
        for c in tournament_repository.get_contestants_for_match(match.id)
    ]


def test_rr_groups_to_de_podium(tournament, users):
    admin = users[0]
    user_of = {
        str(p.id): u
        for p in tournament_repository.get_participants_for_tournament(
            tournament.id
        )
        for u in users
        if u.id == p.user_id
    }

    board = tournament_seeding_service.get_board(
        tournament.id, initiator_id=admin.id
    ).unwrap()
    tournament_seeding_service.generate_from_seeding(
        tournament.id, expected_version=board.version, initiator_id=admin.id
    ).unwrap()
    tournament_service.change_status(
        tournament.id, TournamentStatus.ONGOING, admin.id
    ).unwrap()

    # Group play: the stronger (lower) ID wins, so there is no tie.
    for match in _matches(tournament, 1):
        stronger, weaker = sorted(_contestants(match))
        margin = 2 + match.group_order
        tournament_match_service.admin_set_and_confirm_match(
            match.id, admin.id, {UUID(stronger): margin, UUID(weaker): 0}
        ).unwrap()
    draft = tournament_seeding_service.ensure_playoff_draft(
        tournament.id
    ).unwrap()
    tournament_qualification_service.release_playoffs(
        tournament.id, expected_version=draft.version, initiator_id=admin.id
    ).unwrap()

    # Phase 2: the weaker side wins, so no group table predicts it.
    losers = {}
    for _ in range(12):
        ready = [
            m
            for m in _matches(tournament, 2)
            if m.confirmed_by is None and len(_contestants(m)) == 2
        ]
        if not ready:
            break
        for match in ready:
            stronger, weaker = sorted(_contestants(match))
            tournament_match_service.admin_set_and_confirm_match(
                match.id, admin.id, {UUID(weaker): 3, UUID(stronger): 1}
            ).unwrap()
            losers[match.id] = stronger

    found = tournament_repository.get_tournament(tournament.id)
    assert found.tournament_status is TournamentStatus.COMPLETED
    confirmed = [m for m in _matches(tournament, 2) if m.confirmed_by]
    last_grand_final = max(
        (m for m in confirmed if m.bracket is Bracket.GRAND_FINAL),
        key=lambda m: m.round or 0,
    )
    losers_final = max(
        (m for m in confirmed if m.bracket is Bracket.LOSERS),
        key=lambda m: m.round or 0,
    )
    podium = tournament_service.resolve_podium_display_names(found)
    assert podium == {
        'runner_up': user_of[losers[last_grand_final.id]].screen_name,
        'bronze': user_of[losers[losers_final.id]].screen_name,
    }
    assert (
        podium['runner_up']
        != user_of[str(found.winner_participant_id)].screen_name
    )


def test_completed_before_phase_two_has_an_empty_podium(tournament, users):
    admin = users[0]
    board = tournament_seeding_service.get_board(
        tournament.id, initiator_id=admin.id
    ).unwrap()
    tournament_seeding_service.generate_from_seeding(
        tournament.id, expected_version=board.version, initiator_id=admin.id
    ).unwrap()
    tournament_service.change_status(
        tournament.id, TournamentStatus.ONGOING, admin.id
    ).unwrap()
    for match in _matches(tournament, 1):
        stronger, weaker = sorted(_contestants(match))
        margin = 2 + match.group_order
        tournament_match_service.admin_set_and_confirm_match(
            match.id, admin.id, {UUID(stronger): margin, UUID(weaker): 0}
        ).unwrap()
    assert not _matches(tournament, 2)

    completed = tournament_service.change_status(
        tournament.id, TournamentStatus.COMPLETED, admin.id
    )

    assert completed.is_ok(), completed.unwrap_err()
    found = tournament_repository.get_tournament(tournament.id)
    assert found.tournament_status is TournamentStatus.COMPLETED
    assert tournament_service.resolve_podium_display_names(found) == {
        'runner_up': None,
        'bronze': None,
    }
