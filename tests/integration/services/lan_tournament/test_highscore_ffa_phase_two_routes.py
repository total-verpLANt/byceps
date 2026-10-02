"""
tests.integration.services.lan_tournament.test_highscore_ffa_phase_two_routes
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

A highscore tournament with free-for-all playoffs is playable through the
admin and site routes: its phase-2 lobbies are placement matches.
"""

from datetime import datetime, UTC
from unittest.mock import Mock

import pytest

from byceps.database import db
from byceps.services.lan_tournament import (
    tournament_log_service,
    tournament_match_service,
    tournament_orga_service,
    tournament_repository,
    tournament_score_service,
    tournament_service,
)
from byceps.services.lan_tournament.models import ContestantType
from byceps.services.lan_tournament.models.bracket import Bracket
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
from byceps.util.uuid import generate_uuid7

from tests.helpers import http_client, log_in_user
from tests.integration.services.lan_tournament.test_ffa_waiting_winner import (  # noqa: F401
    engine_admin,
    engine_party,
    engine_players,
    lobbies,
    make_engine,
    play,
)


ADMIN_URL = 'http://admin.acmecon.test/lan-tournaments'
SITE_URL = 'http://www.acmecon.test/lan-tournaments'

PLACEMENTS_ONLY_ERROR = 'Placements apply only to free-for-all matches.'
BRACKET_UNCONFIRM_ERROR = 'Bracket matches are retracted'
FFA_CORRECTION_ERROR = (
    'Free-for-all matches are corrected by unconfirming '
    'them and re-entering the placements.'
)

SE = EliminationMode.SINGLE_ELIMINATION
DE = EliminationMode.DOUBLE_ELIMINATION


@pytest.fixture(scope='module')
def orga(make_user):
    user = make_user(f'HsFfaOrga{str(generate_uuid7())[:12]}')
    log_in_user(user.id)
    return user


@pytest.fixture(scope='module')
def admin(make_admin):
    user = make_admin(
        {'admin.access', 'lan_tournament.administrate', 'lan_tournament.view'}
    )
    log_in_user(user.id)
    return user


@pytest.fixture(scope='module')
def players(make_user):
    suffix = str(generate_uuid7())[:12]
    return [make_user(f'HsFfaP{suffix}{i}') for i in range(8)]


def _create(party, players, orga, *, mode, qualifiers):
    tournament, _ = tournament_service.create_tournament(
        party.id,
        f'HS FFA Routes {generate_uuid7()}',
        contestant_type=ContestantType.SOLO,
        point_table=[5, 3, 2, 1],
        group_size_min=2,
        group_size_max=4,
        advancement_count=1,
        game_format=GameFormat.HIGHSCORE,
        elimination_mode=EliminationMode.NONE,
        score_ordering=ScoreOrdering.HIGHER_IS_BETTER,
        tournament_status=TournamentStatus.ONGOING,
        playoff_game_format=GameFormat.FREE_FOR_ALL,
        playoff_elimination_mode=mode,
        playoff_qualifier_count=qualifiers,
        playoff_release_mode=PlayoffReleaseMode.AUTOMATIC,
    ).unwrap()
    tournament_orga_service.assign_orga(
        tournament.id, orga.id, orga.id
    ).unwrap()
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
    for i, participant in enumerate(
        tournament_repository.get_participants_for_tournament(tournament.id),
        start=1,
    ):
        tournament_score_service.submit_score(
            tournament.id, i * 10, participant_id=participant.id
        ).unwrap()
    tournament_score_service.close_leaderboard(
        tournament.id, initiator_id=orga.id
    ).unwrap()
    return tournament


@pytest.fixture(params=[SE, DE], ids=['SE', 'DE'])
def hs_tournament(request, party, players, orga):
    mode = request.param
    qualifiers = 8 if mode is SE else 4
    yield _create(party, players, orga, mode=mode, qualifiers=qualifiers)
    db.session.rollback()


@pytest.fixture
def hs_se_tournament(party, players, orga):
    yield _create(party, players, orga, mode=SE, qualifiers=8)
    db.session.rollback()


def _first_lobby(tournament):
    return sorted(
        tournament_repository.get_matches_for_round(tournament.id, 0),
        key=lambda m: m.group_order or 0,
    )[0]


def _member_ids(match):
    return sorted(
        str(c.participant_id)
        for c in tournament_match_service.get_contestants_for_match(match.id)
    )


def _confirm(lobby, initiator):
    order = _member_ids(lobby)
    tournament_match_service.set_ffa_placements(
        lobby.id, {cid: i + 1 for i, cid in enumerate(order)}
    ).unwrap()
    tournament_match_service.confirm_ffa_match(lobby.id, initiator.id).unwrap()


def _post(app, user, url, data):
    with http_client(app, user_id=user.id) as client:
        response = client.post(url, data=data)
        with client.session_transaction() as session:
            flashes = session.get('_flashes') or []
    return response, ' | '.join(str(message) for _, message in flashes)


def _is_confirmed(match):
    return tournament_repository.get_match(match.id).confirmed_by is not None


def test_site_orga_submit_confirms_a_phase_two_lobby(
    site_app, hs_tournament, orga
):
    lobby = _first_lobby(hs_tournament)
    assert lobby.phase == 2
    form = {
        f'placement_{cid}': str(i + 1)
        for i, cid in enumerate(_member_ids(lobby))
    }

    _, flashes = _post(
        site_app,
        orga,
        f'{SITE_URL}/orga/matches/{lobby.id}/submit_ffa_result',
        form,
    )

    assert PLACEMENTS_ONLY_ERROR not in flashes
    assert _is_confirmed(lobby)
    placements = {
        str(c.participant_id): c.placement
        for c in tournament_match_service.get_contestants_for_match(lobby.id)
    }
    assert sorted(placements.values()) == [1, 2, 3, 4]


def test_site_orga_unconfirm_reopens_a_phase_two_lobby(
    site_app, hs_se_tournament, orga
):
    lobby = _first_lobby(hs_se_tournament)
    _confirm(lobby, orga)
    assert _is_confirmed(lobby)

    _, flashes = _post(
        site_app,
        orga,
        f'{SITE_URL}/orga/matches/{lobby.id}/unconfirm',
        {'reason': 'wrong placements'},
    )

    assert BRACKET_UNCONFIRM_ERROR not in flashes
    assert not _is_confirmed(lobby)
    retracted = [
        e
        for e in tournament_log_service.get_entries_for_tournament(
            hs_se_tournament.id
        )
        if e.event_type == 'match-result-retracted'
    ]
    assert len(retracted) == 1


def test_admin_unconfirm_reopens_a_phase_two_lobby(
    admin_app, hs_se_tournament, admin
):
    lobby = _first_lobby(hs_se_tournament)
    _confirm(lobby, admin)
    assert _is_confirmed(lobby)

    _, flashes = _post(
        admin_app,
        admin,
        f'{ADMIN_URL}/matches/{lobby.id}/unconfirm',
        {'reason': 'wrong placements'},
    )

    assert BRACKET_UNCONFIRM_ERROR not in flashes
    assert not _is_confirmed(lobby)


def test_admin_correction_refers_a_phase_two_lobby_to_unconfirm(
    admin_app, hs_se_tournament, admin, monkeypatch
):
    lobby = _first_lobby(hs_se_tournament)
    _confirm(lobby, admin)
    spy = Mock(wraps=tournament_match_service.correct_match_result)
    monkeypatch.setattr(tournament_match_service, 'correct_match_result', spy)

    _, flashes = _post(
        admin_app,
        admin,
        f'{ADMIN_URL}/matches/{lobby.id}/correct_result',
        {'reason': 'wrong placements'},
    )

    assert FFA_CORRECTION_ERROR in flashes
    spy.assert_not_called()


def test_site_correction_refers_a_phase_two_lobby_to_unconfirm(
    site_app, hs_se_tournament, orga, monkeypatch
):
    lobby = _first_lobby(hs_se_tournament)
    _confirm(lobby, orga)
    spy = Mock(wraps=tournament_match_service.correct_match_result)
    monkeypatch.setattr(tournament_match_service, 'correct_match_result', spy)

    _, flashes = _post(
        site_app,
        orga,
        f'{SITE_URL}/orga/matches/{lobby.id}/correct_result',
        {'reason': 'wrong placements'},
    )

    assert FFA_CORRECTION_ERROR in flashes
    spy.assert_not_called()


def test_admin_match_page_offers_placements_for_a_phase_two_lobby(
    admin_app, hs_se_tournament, admin
):
    lobby = _first_lobby(hs_se_tournament)

    with http_client(admin_app, user_id=admin.id) as client:
        response = client.get(f'{ADMIN_URL}/matches/{lobby.id}')

    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert 'set_ffa_placements' in html
    assert 'name="score_' not in html
    assert "name='score_" not in html


def test_site_match_page_offers_the_ffa_submit_to_orgas_and_no_score_form_to_players(
    site_app, hs_se_tournament, orga, players
):
    lobby = _first_lobby(hs_se_tournament)
    member = tournament_match_service.get_contestants_for_match(lobby.id)[0]
    participant = tournament_repository.get_participant(member.participant_id)
    player = next(p for p in players if p.id == participant.user_id)
    log_in_user(player.id)

    with http_client(site_app, user_id=orga.id) as client:
        orga_html = client.get(f'{SITE_URL}/matches/{lobby.id}').get_data(
            as_text=True
        )
    with http_client(site_app, user_id=player.id) as client:
        player_response = client.get(f'{SITE_URL}/matches/{lobby.id}')

    assert 'submit_ffa_result' in orga_html
    assert player_response.status_code == 200
    assert '/set_score' not in player_response.get_data(as_text=True)


def test_admin_grand_final_route_reaches_the_service_for_highscore_de(
    admin_app,
    make_engine,  # noqa: F811
    engine_admin,  # noqa: F811
    admin,
    monkeypatch,
):
    tournament = make_engine('highscore', size=4, cut=1)
    for match in lobbies(tournament, Bracket.WINNERS, 0):
        play(match, engine_admin)
    tournament_match_service.advance_ffa_round(
        tournament.id, pool=Bracket.WINNERS
    ).unwrap()
    service = Mock(wraps=tournament_match_service.generate_ffa_grand_final)
    monkeypatch.setattr(
        tournament_match_service, 'generate_ffa_grand_final', service
    )

    _post(
        admin_app,
        admin,
        f'{ADMIN_URL}/tournaments/{tournament.id}/generate_ffa_grand_final',
        {},
    )

    service.assert_called_once()
    assert service.call_args.args[0] == tournament.id
    finals = [
        m
        for m in tournament_repository.get_matches_for_tournament(tournament.id)
        if m.bracket is Bracket.GRAND_FINAL
    ]
    assert len(finals) == 1
    assert finals[0].phase == 2


def test_admin_overview_shows_pool_status_for_highscore_de(
    admin_app,
    make_engine,  # noqa: F811
    admin,
):
    tournament = make_engine('highscore', size=4, cut=1)

    with http_client(admin_app, user_id=admin.id) as client:
        response = client.get(f'{ADMIN_URL}/tournaments/{tournament.id}')

    assert response.status_code == 200
    assert 'Double Elimination Pool Status' in response.get_data(as_text=True)


def test_group_match_of_a_round_robin_still_goes_through_the_correction(
    admin_app, party, players, admin
):
    tournament, _ = tournament_service.create_tournament(
        party.id,
        f'RR Group Regression {generate_uuid7()}',
        contestant_type=ContestantType.SOLO,
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.ROUND_ROBIN,
        tournament_status=TournamentStatus.ONGOING,
        playoff_game_format=GameFormat.ONE_V_ONE,
        playoff_elimination_mode=SE,
        playoff_group_count=2,
        playoff_qualifiers_per_group=2,
        playoff_release_mode=PlayoffReleaseMode.MANUAL,
    ).unwrap()
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
    tournament_match_service.generate_round_robin_bracket(
        tournament.id
    ).unwrap()
    group_match = sorted(
        tournament_repository.get_matches_for_tournament(tournament.id),
        key=lambda m: (m.group_order or 0, m.match_order),
    )[0]
    assert group_match.phase == 1
    first, second = tournament_match_service.get_contestants_for_match(
        group_match.id
    )
    tournament_match_service.set_score(
        group_match.id, first.participant_id, 3
    ).unwrap()
    tournament_match_service.set_score(
        group_match.id, second.participant_id, 1
    ).unwrap()
    tournament_match_service.confirm_match(group_match.id, admin.id).unwrap()
    assert _is_confirmed(group_match)

    _, flashes = _post(
        admin_app,
        admin,
        f'{ADMIN_URL}/matches/{group_match.id}/unconfirm',
        {'reason': 'regression'},
    )

    assert BRACKET_UNCONFIRM_ERROR in flashes
    assert _is_confirmed(group_match)
