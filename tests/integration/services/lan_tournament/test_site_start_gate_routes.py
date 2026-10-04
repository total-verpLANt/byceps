"""
tests.integration.services.lan_tournament.test_site_start_gate_routes
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Drives the orga start button and the orga start route of the site
tournament page for a board that matches, differs from or never reached the
generation.
"""

from datetime import datetime, UTC
from itertools import count

import pytest
from sqlalchemy import text

from byceps.database import db
from byceps.services.lan_tournament import (
    tournament_match_service,
    tournament_orga_service,
    tournament_repository,
    tournament_seeding_repository,
    tournament_seeding_service as svc,
    tournament_service,
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


BASE_URL = 'http://www.acmecon.test/lan-tournaments'

CONFIRM_TEXT = 'Starting uses the generated layout'

_counter = count(1)


@pytest.fixture(scope='module')
def players(make_user):
    return [make_user(f'SiteStartGatePlayer{i}') for i in range(8)]


@pytest.fixture(scope='module')
def orga(make_user):
    user = make_user('SiteStartGateOrga')
    log_in_user(user.id)
    return user


@pytest.fixture
def make_tournament(party, players, orga):
    created = []

    def _make(elimination_mode=EliminationMode.SINGLE_ELIMINATION, **extra):
        result = tournament_service.create_tournament(
            party.id,
            f'Site Start Gate Tournament {next(_counter)}',
            contestant_type=ContestantType.SOLO,
            game_format=GameFormat.ONE_V_ONE,
            elimination_mode=elimination_mode,
            tournament_status=TournamentStatus.REGISTRATION_CLOSED,
            **extra,
        )
        tournament, _ = result.unwrap()
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
        tournament_orga_service.assign_orga(
            tournament.id, orga.id, orga.id
        ).unwrap()
        return tournament

    yield _make
    db.session.rollback()
    for tournament in created:
        if tournament_repository.find_tournament(tournament.id) is not None:
            tournament_service.delete_tournament(tournament.id)


def _view(tournament):
    return f'{BASE_URL}/{tournament.id}'


def _start(tournament):
    return f'{BASE_URL}/orga/tournaments/{tournament.id}/start'


def _generate(tournament, orga):
    board = svc.get_board(tournament.id).unwrap()
    generated = svc.generate_from_seeding(
        tournament.id, expected_version=board.version, initiator_id=orga.id
    )
    assert generated.is_ok(), generated.unwrap_err()


def _change_board(tournament, orga):
    board = svc.get_board(tournament.id).unwrap()
    changed = svc.apply_action(
        tournament.id,
        svc.INITIAL_TARGET,
        svc.Swap(0, 1),
        expected_version=board.version,
        initiator_id=orga.id,
    )
    assert changed.is_ok(), changed.unwrap_err()
    after = svc.get_board(tournament.id).unwrap()
    assert after.generation is svc.GenerationStatus.DIFFERS


def _status(tournament):
    db.session.rollback()
    return tournament_repository.get_tournament(tournament.id).tournament_status


def _writes(tournament):
    db.session.rollback()
    params = {'t': str(tournament.id)}
    return (
        db.session.execute(
            text(
                'select count(*) from lan_tournament_seedings'
                ' where tournament_id = :t'
            ),
            params,
        ).scalar(),
        db.session.execute(
            text(
                'select count(*) from lan_tournament_log_entries'
                ' where tournament_id = :t'
            ),
            params,
        ).scalar(),
    )


def _generate_legacy(tournament, initiator):
    """Generate the bracket without a seeding row, as before the seeding."""
    result = tournament_match_service.generate_single_elimination_bracket(
        tournament.id, initiator_id=initiator.id
    )
    assert result.is_ok(), result.unwrap_err()
    assert _writes(tournament)[0] == 0


def _layout(tournament):
    db.session.rollback()
    contestants = tournament_repository.get_contestants_for_tournament(
        tournament.id
    )
    return sorted(
        sorted(str(c.participant_id) for c in members)
        for members in contestants.values()
    )


def _get(site_app, orga, url):
    with http_client(site_app, user_id=orga.id) as client:
        return client.get(url)


def _post(site_app, orga, url, **data):
    with http_client(site_app, user_id=orga.id) as client:
        return client.post(url, data=data)


def test_a_board_that_matches_keeps_the_plain_start_button(
    site_app, orga, make_tournament
):
    tournament = make_tournament()
    _generate(tournament, orga)

    html = _get(site_app, orga, _view(tournament)).get_data(as_text=True)

    assert f'/orga/tournaments/{tournament.id}/start"' in html
    assert 'data-lt-start-gate' not in html


def test_a_changed_board_asks_for_the_confirmation(
    site_app, orga, make_tournament
):
    tournament = make_tournament()
    _generate(tournament, orga)
    _change_board(tournament, orga)

    html = _get(site_app, orga, _view(tournament)).get_data(as_text=True)

    assert 'data-lt-start-gate="confirm"' in html
    assert 'name="confirm_generated_layout"' in html
    assert CONFIRM_TEXT in html
    assert f'/orga/tournaments/{tournament.id}/seeding' in html


def test_a_start_without_the_confirmation_is_refused(
    site_app, orga, make_tournament
):
    tournament = make_tournament()
    _generate(tournament, orga)
    _change_board(tournament, orga)
    layout = _layout(tournament)

    response = _post(site_app, orga, _start(tournament))

    assert response.status_code == 302
    assert _status(tournament) is TournamentStatus.REGISTRATION_CLOSED
    assert _layout(tournament) == layout


def test_a_confirmed_start_uses_the_generated_layout(
    site_app, orga, make_tournament
):
    tournament = make_tournament()
    _generate(tournament, orga)
    layout = _layout(tournament)
    generated_code = tournament_seeding_repository.find_seeding(
        tournament.id, svc.INITIAL_TARGET
    ).generated_seed_code
    _change_board(tournament, orga)

    response = _post(
        site_app, orga, _start(tournament), confirm_generated_layout='1'
    )

    assert response.status_code == 302
    assert _status(tournament) is TournamentStatus.ONGOING
    assert _layout(tournament) == layout
    db.session.rollback()
    stored = tournament_seeding_repository.find_seeding(
        tournament.id, svc.INITIAL_TARGET
    )
    assert stored.generated_seed_code == generated_code


def test_a_board_without_generation_disables_the_start(
    site_app, orga, make_tournament
):
    tournament = make_tournament()
    svc.get_board(tournament.id).unwrap()

    html = _get(site_app, orga, _view(tournament)).get_data(as_text=True)

    assert 'data-lt-start-gate="blocked"' in html
    start = html.index('data-lt-start-gate="blocked"')
    gate = html[start : start + 900]
    assert 'disabled' in gate
    assert 'Generate brackets first.' in gate
    assert f'/orga/tournaments/{tournament.id}/seeding' in gate
    assert f'/orga/tournaments/{tournament.id}/start' not in html

    response = _post(site_app, orga, _start(tournament))

    assert response.status_code == 302
    assert _status(tournament) is TournamentStatus.REGISTRATION_CLOSED


def test_a_resume_without_the_confirmation_is_refused(
    site_app, orga, make_tournament
):
    tournament = make_tournament()
    _generate(tournament, orga)
    _change_board(tournament, orga)
    before = _writes(tournament)
    layout = _layout(tournament)
    url = f'{BASE_URL}/orga/tournaments/{tournament.id}/resume'

    response = _post(site_app, orga, url)

    assert response.status_code == 302
    assert _status(tournament) is TournamentStatus.REGISTRATION_CLOSED
    assert _writes(tournament) == before
    assert _layout(tournament) == layout


def test_a_confirmed_resume_starts(site_app, orga, make_tournament):
    tournament = make_tournament()
    _generate(tournament, orga)
    _change_board(tournament, orga)
    layout = _layout(tournament)
    url = f'{BASE_URL}/orga/tournaments/{tournament.id}/resume'

    response = _post(site_app, orga, url, confirm_generated_layout='1')

    assert response.status_code == 302
    assert _status(tournament) is TournamentStatus.ONGOING
    assert _layout(tournament) == layout


def test_a_legacy_tournament_without_a_board_keeps_the_plain_start(
    site_app, orga, make_tournament
):
    tournament = make_tournament()
    _generate_legacy(tournament, orga)
    before = _writes(tournament)

    html = _get(site_app, orga, _view(tournament)).get_data(as_text=True)

    assert _writes(tournament) == before
    assert 'data-lt-start-gate' not in html
    assert f'/orga/tournaments/{tournament.id}/start"' in html

    response = _post(site_app, orga, _start(tournament))

    assert response.status_code == 302
    assert _status(tournament) is TournamentStatus.ONGOING
    assert _writes(tournament)[0] == 0


def test_the_page_read_of_a_tournament_with_a_board_writes_nothing(
    site_app, orga, make_tournament
):
    tournament = make_tournament()
    _generate(tournament, orga)
    _change_board(tournament, orga)
    before = _writes(tournament)

    _get(site_app, orga, _view(tournament))

    assert _writes(tournament) == before


def test_site_start_gate_shows_the_group_shortfall(
    site_app, orga, make_tournament
):
    tournament = make_tournament(
        EliminationMode.ROUND_ROBIN,
        playoff_game_format=GameFormat.ONE_V_ONE,
        playoff_elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        playoff_group_count=6,
        playoff_qualifiers_per_group=1,
        playoff_release_mode=PlayoffReleaseMode.MANUAL,
    )

    _generate(tournament, orga)

    html = _get(site_app, orga, _view(tournament)).get_data(as_text=True)

    assert 'The roster gives 4 groups instead of the configured 6.' in html
    assert 'Only 4 contestants qualify instead of the configured 6.' in html
    assert f'/orga/tournaments/{tournament.id}/start"' in html
    assert 'data-lt-start-gate="blocked"' not in html


def test_site_start_gate_shows_no_shortfall_for_a_full_roster(
    site_app, orga, make_tournament
):
    tournament = make_tournament(
        EliminationMode.ROUND_ROBIN,
        playoff_game_format=GameFormat.ONE_V_ONE,
        playoff_elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        playoff_group_count=4,
        playoff_qualifiers_per_group=1,
        playoff_release_mode=PlayoffReleaseMode.MANUAL,
    )

    html = _get(site_app, orga, _view(tournament)).get_data(as_text=True)

    assert 'instead of the configured' not in html
