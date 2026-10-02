"""
tests.integration.services.lan_tournament.test_seeding_page_states
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Drives the seeding pages before registration closes and the flash that names
what a generation created.
"""

from datetime import datetime, UTC
from itertools import count

import pytest

from byceps.database import db
from byceps.services.lan_tournament import (
    tournament_match_service,
    tournament_orga_service,
    tournament_repository,
    tournament_seeding_service as svc,
    tournament_service,
)
from byceps.services.lan_tournament.lan_tournament_view_helpers import (
    generation_flash,
)
from byceps.services.lan_tournament.models import ContestantType
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
from byceps.util.uuid import generate_uuid7

from tests.helpers import http_client, log_in_user


ADMIN_URL = 'http://admin.acmecon.test/lan-tournaments'
SITE_URL = 'http://www.acmecon.test/lan-tournaments'

EMPTY_STATE = 'The seeding opens once registration is closed.'

_counter = count(1)


@pytest.fixture(scope='module')
def players(make_user):
    return [make_user(f'SeedingPageStatePlayer{i}') for i in range(8)]


@pytest.fixture(scope='module')
def admin(make_admin):
    user = make_admin({'admin.access', 'lan_tournament.administrate'})
    log_in_user(user.id)
    return user


@pytest.fixture(scope='module')
def orga(make_user):
    user = make_user('SeedingPageStateOrga')
    log_in_user(user.id)
    return user


@pytest.fixture
def make_tournament(party, players, orga):
    created = []

    def _make(
        status=TournamentStatus.REGISTRATION_CLOSED,
        mode=(GameFormat.ONE_V_ONE, EliminationMode.SINGLE_ELIMINATION),
        participants=8,
        **kwargs,
    ):
        result = tournament_service.create_tournament(
            party.id,
            f'Seeding Page States {next(_counter)}',
            contestant_type=ContestantType.SOLO,
            game_format=mode[0],
            elimination_mode=mode[1],
            tournament_status=status,
            **kwargs,
        )
        assert result.is_ok(), result.unwrap_err()
        tournament, _ = result.unwrap()
        created.append(tournament)
        for user in players[:participants]:
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
        tournament_orga_service.assign_orga(
            tournament.id, orga.id, orga.id
        ).unwrap()
        return tournament

    yield _make
    db.session.rollback()
    for tournament in created:
        if tournament_repository.find_tournament(tournament.id) is not None:
            tournament_service.delete_tournament(tournament.id)


def _get(app, user, url):
    with http_client(app, user_id=user.id) as client:
        return client.get(url)


def _generate(app, user, url, tournament, target='initial'):
    board = svc.get_board(tournament.id, target).unwrap()
    with http_client(app, user_id=user.id) as client:
        return client.post(
            url,
            data={'target': target, 'version': str(board.version)},
            follow_redirects=True,
        )


@pytest.mark.parametrize(
    'status, tag_class',
    [
        (TournamentStatus.REGISTRATION_OPEN, 't-info'),
        (TournamentStatus.DRAFT, 't-mute'),
    ],
)
def test_admin_seeding_before_close_renders_the_empty_state(
    admin_app, admin, make_tournament, status, tag_class
):
    tournament = make_tournament(status=status)

    response = _get(
        admin_app, admin, f'{ADMIN_URL}/tournaments/{tournament.id}/seeding'
    )

    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert EMPTY_STATE in html
    assert f'lt-seed-tag {tag_class}"' in html
    assert 'data-lt-seed-board' not in html


@pytest.mark.parametrize(
    'status, tag_class',
    [
        (TournamentStatus.REGISTRATION_OPEN, 't-info'),
        (TournamentStatus.DRAFT, 't-mute'),
    ],
)
def test_site_seeding_before_close_renders_the_empty_state(
    site_app, orga, make_tournament, status, tag_class
):
    tournament = make_tournament(status=status)

    response = _get(
        site_app, orga, f'{SITE_URL}/orga/tournaments/{tournament.id}/seeding'
    )

    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert EMPTY_STATE in html
    assert f'lt-seed-tag {tag_class}"' in html
    assert 'data-lt-seed-board' not in html


def test_seeding_before_close_stores_no_draft(
    admin_app, admin, make_tournament
):
    tournament = make_tournament(status=TournamentStatus.REGISTRATION_OPEN)

    _get(admin_app, admin, f'{ADMIN_URL}/tournaments/{tournament.id}/seeding')

    db.session.rollback()
    assert (
        svc.tournament_seeding_repository.find_seeding(tournament.id, 'initial')
        is None
    )


def test_admin_unknown_target_still_redirects(
    admin_app, admin, make_tournament
):
    tournament = make_tournament(status=TournamentStatus.REGISTRATION_OPEN)

    response = _get(
        admin_app,
        admin,
        f'{ADMIN_URL}/tournaments/{tournament.id}/seeding?target=nonsense',
    )

    assert response.status_code == 302


def test_generate_flash_names_lobbies_for_ffa(
    admin_app, admin, make_tournament
):
    tournament = make_tournament(
        mode=(GameFormat.FREE_FOR_ALL, EliminationMode.SINGLE_ELIMINATION),
        max_players=16,
        group_size_min=2,
        group_size_max=4,
        advancement_count=2,
        point_table=[3, 2, 1, 1],
    )

    response = _generate(
        admin_app,
        admin,
        f'{ADMIN_URL}/tournaments/{tournament.id}/seeding/generate',
        tournament,
    )

    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert '2 lobbies generated.' in html
    assert 'Bracket generated' not in html


def test_generate_flash_singular_for_one_match(admin_app):
    with admin_app.test_request_context():
        assert (
            generation_flash(_StubTournament(), 'initial', 1)
            == 'Bracket generated with 1 match.'
        )
        assert (
            generation_flash(_StubTournament(), 'initial', 7)
            == 'Bracket generated with 7 matches.'
        )
        assert (
            generation_flash(
                _StubTournament(elimination_mode=EliminationMode.ROUND_ROBIN),
                'initial',
                1,
            )
            == 'Groups generated with 1 match.'
        )


def test_generate_flash_names_the_ffa_round(admin_app):
    with admin_app.test_request_context():
        assert (
            generation_flash(_StubTournament(), 'ffa:SE:1', 1)
            == 'Round 2: 1 lobby generated.'
        )
        assert (
            generation_flash(_StubTournament(), 'ffa:WB:2', 3)
            == 'Round 3: 3 lobbies generated.'
        )


def test_site_match_page_shows_the_phase_label(site_app, orga, make_tournament):
    tournament = make_tournament(
        mode=(GameFormat.ONE_V_ONE, EliminationMode.ROUND_ROBIN),
        playoff_game_format=GameFormat.ONE_V_ONE,
        playoff_elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        playoff_group_count=2,
        playoff_qualifiers_per_group=2,
        playoff_release_mode=PlayoffReleaseMode.MANUAL,
    )
    generated = tournament_match_service.generate_round_robin_bracket(
        tournament.id
    )
    assert generated.is_ok(), generated.unwrap_err()
    matches = tournament_repository.get_matches_for_tournament(tournament.id)
    group_b = next(m for m in matches if m.group_order == 1)

    response = _get(site_app, orga, f'{SITE_URL}/matches/{group_b.id}')

    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "<p class='match-label'>Group B</p>" in html


class _StubTournament:
    def __init__(
        self,
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
    ):
        self.game_format = game_format
        self.elimination_mode = elimination_mode
        self.playoff_game_format = None
        self.playoff_elimination_mode = None
