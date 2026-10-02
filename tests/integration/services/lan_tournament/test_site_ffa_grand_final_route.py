"""
tests.integration.services.lan_tournament.test_site_ffa_grand_final_route
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

A scoped orga generates the grand final of a double-elimination FFA phase
from the site, and sees the offer on the tournament page.
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


SITE_URL = 'http://www.acmecon.test/lan-tournaments'

NOT_DE_ERROR = 'Grand Final is only for double elimination tournaments.'
NOT_FFA_ERROR = 'This tournament is not a Free-for-All format.'
UNCONFIRMED_ERROR = 'Bracket matches are not confirmed.'
NOT_ONGOING_ERROR = 'The tournament must be ongoing to advance an FFA round.'

SE = EliminationMode.SINGLE_ELIMINATION
DE = EliminationMode.DOUBLE_ELIMINATION


@pytest.fixture(scope='module')
def orga(make_user):
    user = make_user(f'GfOrga{str(generate_uuid7())[:12]}')
    log_in_user(user.id)
    return user


@pytest.fixture(scope='module')
def participant_user(make_user):
    user = make_user(f'GfPlayer{str(generate_uuid7())[:12]}')
    log_in_user(user.id)
    return user


@pytest.fixture(scope='module')
def players(make_user):
    suffix = str(generate_uuid7())[:12]
    return [make_user(f'GfP{suffix}{i}') for i in range(4)]


@pytest.fixture
def make_tournament(party, players, orga):
    created = []

    def _create(kind, mode, *, status=TournamentStatus.ONGOING):
        args = dict(
            contestant_type=ContestantType.SOLO,
            point_table=[5, 3, 2, 1],
            group_size_min=2,
            group_size_max=4,
            advancement_count=1,
        )
        if kind == 'plain':
            args.update(
                game_format=GameFormat.FREE_FOR_ALL,
                elimination_mode=mode,
                tournament_status=TournamentStatus.REGISTRATION_CLOSED,
                max_players=16,
            )
        else:
            args.update(
                game_format=GameFormat.HIGHSCORE,
                elimination_mode=EliminationMode.NONE,
                score_ordering=ScoreOrdering.HIGHER_IS_BETTER,
                tournament_status=TournamentStatus.ONGOING,
                playoff_game_format=GameFormat.FREE_FOR_ALL,
                playoff_elimination_mode=mode,
                playoff_qualifier_count=4,
                playoff_release_mode=PlayoffReleaseMode.AUTOMATIC,
            )
        tournament, _ = tournament_service.create_tournament(
            party.id, f'GF Route {generate_uuid7()}', **args
        ).unwrap()
        created.append(tournament)
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
        if kind == 'plain':
            board = tournament_seeding_service.get_board(
                tournament.id, initiator_id=orga.id
            ).unwrap()
            tournament_seeding_service.generate_from_seeding(
                tournament.id,
                expected_version=board.version,
                initiator_id=orga.id,
            ).unwrap()
            tournament_service.change_status(
                tournament.id, TournamentStatus.ONGOING, orga.id
            ).unwrap()
        else:
            for i, participant in enumerate(
                tournament_repository.get_participants_for_tournament(
                    tournament.id
                ),
                start=1,
            ):
                tournament_score_service.submit_score(
                    tournament.id, i * 10, participant_id=participant.id
                ).unwrap()
            tournament_score_service.close_leaderboard(
                tournament.id, initiator_id=orga.id
            ).unwrap()
        return tournament

    yield _create
    db.session.rollback()
    for tournament in created:
        if tournament_repository.find_tournament(tournament.id) is not None:
            tournament_service.delete_tournament(tournament.id)


def _lobbies(tournament, bracket, round_number):
    return sorted(
        tournament_repository.get_matches_for_round(
            tournament.id, round_number, bracket=bracket
        ),
        key=lambda m: m.group_order or 0,
    )


def _play(match, initiator):
    order = sorted(
        str(c.participant_id)
        for c in tournament_match_service.get_contestants_for_match(match.id)
    )
    tournament_match_service.set_ffa_placements(
        match.id, {cid: i + 1 for i, cid in enumerate(order)}
    ).unwrap()
    tournament_match_service.confirm_ffa_match(match.id, initiator.id).unwrap()


def _make_gf_eligible(tournament, initiator):
    for match in _lobbies(tournament, Bracket.WINNERS, 0):
        _play(match, initiator)
    tournament_match_service.advance_ffa_round(
        tournament.id, pool=Bracket.WINNERS
    ).unwrap()
    # The 'grand_final_eligible' answer returns without commit or rollback and
    # keeps the tournament row lock; release it before a request takes it.
    db.session.rollback()


def _grand_finals(tournament):
    return [
        m
        for m in tournament_repository.get_matches_for_tournament(tournament.id)
        if m.bracket is Bracket.GRAND_FINAL
    ]


def _post(app, user, tournament, data=None):
    url = (
        f'{SITE_URL}/orga/tournaments/{tournament.id}/generate_ffa_grand_final'
    )
    with http_client(app, user_id=user.id) as client:
        response = client.post(url, data=data or {})
        with client.session_transaction() as session:
            flashes = session.get('_flashes') or []
    return response, ' | '.join(str(message) for _, message in flashes)


def _gf_entries(tournament):
    return [
        e
        for e in tournament_log_service.get_entries_for_tournament(
            tournament.id
        )
        if e.event_type == 'bracket-generated'
        and e.data.get('target') == 'ffa:GF'
    ]


@pytest.mark.parametrize('kind', ['highscore', 'plain'])
def test_orga_generates_the_grand_final(site_app, make_tournament, orga, kind):
    tournament = make_tournament(kind, DE)
    _make_gf_eligible(tournament, orga)

    response, flashes = _post(site_app, orga, tournament)

    assert response.status_code == 302
    assert response.location.endswith(
        f'/lan-tournaments/{tournament.id}/bracket'
    )
    assert 'Grand Final generated with 1 group(s).' in flashes
    finals = _grand_finals(tournament)
    assert len(finals) == 1
    assert finals[0].phase == (2 if kind == 'highscore' else 1)
    db.session.rollback()
    entries = _gf_entries(tournament)
    assert len(entries) == 1
    assert entries[0].initiator_id == orga.id


def test_grand_final_route_refuses_single_elimination(
    site_app, make_tournament, orga, monkeypatch
):
    tournament = make_tournament('highscore', SE)
    service = Mock(wraps=tournament_match_service.generate_ffa_grand_final)
    monkeypatch.setattr(
        tournament_match_service, 'generate_ffa_grand_final', service
    )

    response, flashes = _post(site_app, orga, tournament)

    assert response.status_code == 302
    assert response.location.endswith(f'/lan-tournaments/{tournament.id}')
    assert NOT_DE_ERROR in flashes
    service.assert_not_called()


def test_grand_final_route_refuses_a_format_without_lobbies(
    site_app, party, orga
):
    tournament, _ = tournament_service.create_tournament(
        party.id,
        f'GF Route RR {generate_uuid7()}',
        contestant_type=ContestantType.SOLO,
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.ROUND_ROBIN,
        tournament_status=TournamentStatus.ONGOING,
    ).unwrap()
    tournament_orga_service.assign_orga(
        tournament.id, orga.id, orga.id
    ).unwrap()

    response, flashes = _post(site_app, orga, tournament)

    assert response.status_code == 302
    assert NOT_FFA_ERROR in flashes
    assert not _grand_finals(tournament)
    db.session.rollback()
    tournament_service.delete_tournament(tournament.id)


def test_grand_final_route_surfaces_the_service_refusal(
    site_app, make_tournament, orga
):
    tournament = make_tournament('highscore', DE)

    response, flashes = _post(site_app, orga, tournament)

    assert response.status_code == 302
    assert UNCONFIRMED_ERROR in flashes
    assert not _grand_finals(tournament)


def test_grand_final_route_is_not_shadowed_by_the_status_action_route(
    site_app, make_tournament, orga
):
    tournament = make_tournament('highscore', DE)
    _make_gf_eligible(tournament, orga)
    tournament_service.change_status(
        tournament.id, TournamentStatus.PAUSED, orga.id
    ).unwrap()

    response, flashes = _post(site_app, orga, tournament)

    assert response.status_code == 302
    assert NOT_ONGOING_ERROR in flashes
    assert not _grand_finals(tournament)


def test_view_page_offers_the_grand_final_to_orgas_only(
    site_app, make_tournament, orga, participant_user
):
    tournament = make_tournament('highscore', DE)
    _make_gf_eligible(tournament, orga)
    url = f'{SITE_URL}/{tournament.id}'
    action = f'/orga/tournaments/{tournament.id}/generate_ffa_grand_final'

    with http_client(site_app, user_id=orga.id) as client:
        orga_html = client.get(url).get_data(as_text=True)
    with http_client(site_app, user_id=participant_user.id) as client:
        player_html = client.get(url).get_data(as_text=True)
    with http_client(site_app) as client:
        anonymous_html = client.get(url).get_data(as_text=True)

    assert 'data-lt-gf="ready"' in orga_html
    assert action in orga_html
    for html in (player_html, anonymous_html):
        assert 'data-lt-gf' not in html
        assert action not in html


def test_view_page_links_the_existing_grand_final(
    site_app, make_tournament, orga
):
    tournament = make_tournament('highscore', DE)
    _make_gf_eligible(tournament, orga)
    tournament_match_service.generate_ffa_grand_final(
        tournament.id, initiator_id=orga.id
    ).unwrap()
    final = _grand_finals(tournament)[0]

    with http_client(site_app, user_id=orga.id) as client:
        html = client.get(f'{SITE_URL}/{tournament.id}').get_data(as_text=True)

    assert 'data-lt-gf="exists"' in html
    assert f'/matches/{final.id}' in html
    assert 'advance_ffa_round' not in html
