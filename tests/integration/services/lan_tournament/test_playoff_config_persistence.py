"""
tests.integration.services.lan_tournament.test_playoff_config_persistence
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from dataclasses import replace
from datetime import datetime, UTC

import pytest
from sqlalchemy.exc import IntegrityError

from byceps.database import db
from byceps.services.lan_tournament import tournament_repository
from byceps.services.lan_tournament.models import ContestantType
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.playoff import PlayoffReleaseMode
from byceps.services.lan_tournament.models.tournament import (
    Tournament,
    TournamentID,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipant,
    TournamentParticipantID,
)
from byceps.services.party.models import PartyID
from byceps.util.uuid import generate_uuid7


PARTY_ID = PartyID('lan-party-2024-playoff-config')

NOW = datetime(2024, 1, 1, 12, 0, 0)


@pytest.fixture(scope='module')
def party(make_party, make_brand):
    brand = make_brand('playoffconfigbrand', 'Playoff Config Brand')
    return make_party(brand, PARTY_ID, 'LAN Party 2024 Playoff Config')


@pytest.fixture(scope='module')
def orga(make_user):
    return make_user('PlayoffConfigOrga')


def _make_tournament(game_format, elimination_mode, **kwargs) -> Tournament:
    fields = {
        'id': TournamentID(generate_uuid7()),
        'party_id': PARTY_ID,
        'name': f'Playoff Config {generate_uuid7()}',
        'game': None,
        'description': None,
        'image_url': None,
        'ruleset': None,
        'start_time': None,
        'created_at': NOW,
        'min_players': None,
        'max_players': None,
        'min_teams': None,
        'max_teams': None,
        'min_players_in_team': None,
        'max_players_in_team': None,
        'contestant_type': ContestantType.SOLO,
        'tournament_status': TournamentStatus.REGISTRATION_OPEN,
        'game_format': game_format,
        'elimination_mode': elimination_mode,
    }
    fields.update(kwargs)
    return Tournament(**fields)


def _find(tournament_id) -> Tournament:
    db.session.expire_all()
    tournament = tournament_repository.find_tournament(tournament_id)
    assert tournament is not None
    return tournament


def test_playoff_config_round_trip(party):
    rr = _make_tournament(
        GameFormat.ONE_V_ONE,
        EliminationMode.ROUND_ROBIN,
        playoff_game_format=GameFormat.ONE_V_ONE,
        playoff_elimination_mode=EliminationMode.DOUBLE_ELIMINATION,
        playoff_group_count=4,
        playoff_qualifiers_per_group=2,
        playoff_release_mode=PlayoffReleaseMode.MANUAL,
    )
    tournament_repository.create_tournament(rr)

    found = _find(rr.id)
    assert found.has_playoffs
    assert found.playoff_game_format is GameFormat.ONE_V_ONE
    assert found.playoff_elimination_mode is EliminationMode.DOUBLE_ELIMINATION
    assert found.playoff_group_count == 4
    assert found.playoff_qualifiers_per_group == 2
    assert found.playoff_qualifier_count is None
    assert found.playoff_release_mode is PlayoffReleaseMode.MANUAL
    assert found.playoff_auto_release_suspended is False
    assert found.playoff_released_at is None
    assert found.playoff_released_by is None
    assert found.leaderboard_closed_at is None

    hs = _make_tournament(GameFormat.HIGHSCORE, EliminationMode.NONE)
    tournament_repository.create_tournament(hs)
    assert not _find(hs.id).has_playoffs

    tournament_repository.update_tournament(
        replace(
            _find(hs.id),
            playoff_game_format=GameFormat.FREE_FOR_ALL,
            playoff_elimination_mode=EliminationMode.SINGLE_ELIMINATION,
            playoff_qualifier_count=8,
            playoff_release_mode=PlayoffReleaseMode.AUTOMATIC,
        )
    )

    found = _find(hs.id)
    assert found.has_playoffs
    assert found.playoff_game_format is GameFormat.FREE_FOR_ALL
    assert found.playoff_qualifier_count == 8
    assert found.playoff_group_count is None
    assert found.playoff_release_mode is PlayoffReleaseMode.AUTOMATIC

    tournament_repository.update_tournament(
        replace(
            found,
            playoff_game_format=None,
            playoff_elimination_mode=None,
            playoff_qualifier_count=None,
            playoff_release_mode=None,
        )
    )
    assert not _find(hs.id).has_playoffs


def test_playoff_config_check_rejects_partial_config(party):
    tournament = _make_tournament(
        GameFormat.ONE_V_ONE,
        EliminationMode.ROUND_ROBIN,
        playoff_game_format=GameFormat.ONE_V_ONE,
        playoff_elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        playoff_group_count=0,
        playoff_qualifiers_per_group=2,
        playoff_release_mode=PlayoffReleaseMode.MANUAL,
    )

    with pytest.raises(IntegrityError) as exc_info:
        tournament_repository.create_tournament(tournament)
    db.session.rollback()

    assert (
        exc_info.value.orig.diag.constraint_name
        == 'ck_lan_tournaments_playoff_config'
    )


def test_update_tournament_does_not_touch_release_fields(party, orga):
    tournament = _make_tournament(
        GameFormat.ONE_V_ONE,
        EliminationMode.ROUND_ROBIN,
        playoff_game_format=GameFormat.ONE_V_ONE,
        playoff_elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        playoff_group_count=2,
        playoff_qualifiers_per_group=1,
        playoff_release_mode=PlayoffReleaseMode.AUTOMATIC,
    )
    tournament_repository.create_tournament(tournament)
    stale = _find(tournament.id)

    tournament_repository.set_playoff_release(
        tournament.id, released_at=NOW, released_by=orga.id
    )
    tournament_repository.set_leaderboard_closed(tournament.id, NOW)
    db.session.commit()

    tournament_repository.update_tournament(replace(stale, name='Renamed'))

    found = _find(tournament.id)
    assert found.name == 'Renamed'
    assert found.playoff_released_at == NOW
    assert found.playoff_released_by == orga.id
    assert found.leaderboard_closed_at == NOW

    tournament_repository.clear_playoff_release(
        tournament.id, suspend_auto=True
    )
    db.session.commit()
    stale_released = replace(
        found,
        playoff_released_at=NOW,
        playoff_auto_release_suspended=False,
    )

    tournament_repository.update_tournament(
        replace(stale_released, name='Renamed again')
    )

    found = _find(tournament.id)
    assert found.name == 'Renamed again'
    assert found.playoff_released_at is None
    assert found.playoff_released_by is None
    assert found.playoff_auto_release_suspended is True


def test_update_tournament_does_not_touch_status_winner_or_position(
    party, orga
):
    tournament = _make_tournament(
        GameFormat.ONE_V_ONE, EliminationMode.SINGLE_ELIMINATION
    )
    tournament_repository.create_tournament(
        replace(tournament, tournament_status=TournamentStatus.ONGOING)
    )
    participant_id = TournamentParticipantID(generate_uuid7())
    tournament_repository.create_participant(
        TournamentParticipant(
            id=participant_id,
            user_id=orga.id,
            tournament_id=tournament.id,
            substitute_player=False,
            team_id=None,
            created_at=datetime.now(UTC),
        )
    )
    db.session.commit()
    stale = _find(tournament.id)
    assert stale.tournament_status is TournamentStatus.ONGOING
    assert stale.winner_participant_id is None

    tournament_repository.set_tournament_status_flush(
        tournament.id, TournamentStatus.COMPLETED
    )
    tournament_repository.set_tournament_winner(
        tournament.id,
        winner_team_id=None,
        winner_participant_id=participant_id,
    )
    tournament_repository.reorder_tournaments(
        [str(generate_uuid7()), str(tournament.id)]
    )
    db.session.commit()

    tournament_repository.update_tournament(replace(stale, name='Renamed'))

    found = _find(tournament.id)
    assert found.name == 'Renamed'
    assert found.tournament_status is TournamentStatus.COMPLETED
    assert found.winner_participant_id == participant_id
    assert found.position == 1
