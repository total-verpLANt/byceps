"""
tests.integration.services.lan_tournament.test_admin_start_gate_routes
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Drives the start button and the start route of the admin tournament page
for a board that matches, differs from or never reached the generation.
"""

from datetime import datetime, UTC
from itertools import count

import pytest
from sqlalchemy import text

from byceps.database import db
from byceps.services.lan_tournament import (
    tournament_match_service,
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

from tests.helpers import log_in_user


BASE_URL = 'http://admin.acmecon.test/lan-tournaments'

PARTY_ID = PartyID('lan-party-admin-start-gate')

CONFIRM_TEXT = 'Starting uses the generated layout'

_counter = count(1)


@pytest.fixture(scope='module')
def party(make_party, make_brand):
    brand = make_brand('adminstartgatebrand', 'Admin Start Gate Brand')
    return make_party(brand, PARTY_ID, 'LAN Party Admin Start Gate')


@pytest.fixture(scope='module')
def users(make_user):
    return [make_user(f'AdminStartGateUser{i}') for i in range(8)]


@pytest.fixture(scope='module')
def admin(make_admin):
    user = make_admin(
        {'admin.access', 'lan_tournament.administrate', 'lan_tournament.view'}
    )
    log_in_user(user.id)
    return user


@pytest.fixture(scope='module')
def client(make_client, admin_app, admin):
    return make_client(admin_app, user_id=admin.id)


@pytest.fixture
def make_tournament(party, users):
    created = []

    def _make(
        mode=(GameFormat.ONE_V_ONE, EliminationMode.SINGLE_ELIMINATION),
        **extra,
    ):
        if mode[0] is GameFormat.HIGHSCORE:
            extra['score_ordering'] = ScoreOrdering.HIGHER_IS_BETTER
        result = tournament_service.create_tournament(
            PARTY_ID,
            f'Admin Start Gate Tournament {next(_counter)}',
            contestant_type=ContestantType.SOLO,
            game_format=mode[0],
            elimination_mode=mode[1],
            tournament_status=TournamentStatus.REGISTRATION_CLOSED,
            **extra,
        )
        assert result.is_ok(), result.unwrap_err()
        tournament, _ = result.unwrap()
        created.append(tournament)
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
        return tournament

    yield _make
    db.session.rollback()
    for tournament in created:
        if tournament_repository.find_tournament(tournament.id) is not None:
            tournament_service.delete_tournament(tournament.id)


def _url(tournament, suffix=''):
    return f'{BASE_URL}/tournaments/{tournament.id}{suffix}'


def _generate(tournament, admin):
    board = svc.get_board(tournament.id).unwrap()
    generated = svc.generate_from_seeding(
        tournament.id, expected_version=board.version, initiator_id=admin.id
    )
    assert generated.is_ok(), generated.unwrap_err()


def _change_board(tournament, admin):
    board = svc.get_board(tournament.id).unwrap()
    changed = svc.apply_action(
        tournament.id,
        svc.INITIAL_TARGET,
        svc.Swap(0, 1),
        expected_version=board.version,
        initiator_id=admin.id,
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


def test_a_board_that_matches_keeps_the_plain_start_button(
    client, admin, make_tournament
):
    tournament = make_tournament()
    _generate(tournament, admin)

    html = client.get(_url(tournament)).get_data(as_text=True)

    assert 'data-lt-start-gate' not in html
    assert f'/tournaments/{tournament.id}/start"' in html
    assert 'confirm_generated_layout' not in html


def test_a_changed_board_asks_for_the_confirmation(
    client, admin, make_tournament
):
    tournament = make_tournament()
    _generate(tournament, admin)
    _change_board(tournament, admin)

    html = client.get(_url(tournament)).get_data(as_text=True)

    assert 'data-lt-start-gate="confirm"' in html
    assert 'name="confirm_generated_layout"' in html
    assert CONFIRM_TEXT in html
    assert f'/tournaments/{tournament.id}/seeding' in html


def test_a_start_without_the_confirmation_is_refused(
    client, admin, make_tournament
):
    tournament = make_tournament()
    _generate(tournament, admin)
    _change_board(tournament, admin)
    layout = _layout(tournament)

    response = client.post(_url(tournament, '/start'))

    assert response.status_code == 302
    assert _status(tournament) is TournamentStatus.REGISTRATION_CLOSED
    assert _layout(tournament) == layout


def test_a_confirmed_start_uses_the_generated_layout(
    client, admin, make_tournament
):
    tournament = make_tournament()
    _generate(tournament, admin)
    layout = _layout(tournament)
    generated_code = tournament_seeding_repository.find_seeding(
        tournament.id, svc.INITIAL_TARGET
    ).generated_seed_code
    _change_board(tournament, admin)

    response = client.post(
        _url(tournament, '/start'), data={'confirm_generated_layout': '1'}
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
    client, admin, make_tournament
):
    tournament = make_tournament()
    svc.get_board(tournament.id).unwrap()

    html = client.get(_url(tournament)).get_data(as_text=True)

    assert 'data-lt-start-gate="blocked"' in html
    start = html.index('data-lt-start-gate="blocked"')
    gate = html[start : start + 900]
    assert 'disabled' in gate
    assert 'Generate brackets first.' in gate
    assert f'/tournaments/{tournament.id}/seeding' in gate
    assert f'/tournaments/{tournament.id}/start' not in html

    response = client.post(_url(tournament, '/start'))

    assert response.status_code == 302
    assert _status(tournament) is TournamentStatus.REGISTRATION_CLOSED


def test_the_page_read_draws_no_board(client, make_tournament):
    tournament = make_tournament()

    client.get(_url(tournament))

    db.session.rollback()
    assert (
        tournament_seeding_repository.find_seeding(
            tournament.id, svc.INITIAL_TARGET
        )
        is None
    )


def test_a_format_without_a_board_keeps_the_plain_start_button(
    client, make_tournament
):
    tournament = make_tournament((GameFormat.HIGHSCORE, EliminationMode.NONE))

    html = client.get(_url(tournament)).get_data(as_text=True)

    assert 'data-lt-start-gate' not in html
    assert f'/tournaments/{tournament.id}/start"' in html


def test_a_roster_change_after_the_generation_disables_the_start(
    client, admin, make_tournament, make_user
):
    tournament = make_tournament()
    _generate(tournament, admin)
    late = make_user('AdminStartGateLate')
    tournament_repository.create_participant(
        TournamentParticipant(
            id=TournamentParticipantID(generate_uuid7()),
            user_id=late.id,
            tournament_id=tournament.id,
            substitute_player=False,
            team_id=None,
            created_at=datetime.now(UTC),
        )
    )
    db.session.commit()

    html = client.get(_url(tournament)).get_data(as_text=True)

    assert 'data-lt-start-gate="blocked"' in html
    assert 'The roster changed after generation.' in html
    assert f'/tournaments/{tournament.id}/start"' not in html


def test_a_legacy_tournament_without_a_board_keeps_the_plain_start(
    client, admin, make_tournament
):
    tournament = make_tournament()
    _generate_legacy(tournament, admin)
    before = _writes(tournament)

    html = client.get(_url(tournament)).get_data(as_text=True)

    assert _writes(tournament) == before
    assert 'data-lt-start-gate' not in html
    assert f'/tournaments/{tournament.id}/start"' in html

    response = client.post(_url(tournament, '/start'))

    assert response.status_code == 302
    assert _status(tournament) is TournamentStatus.ONGOING
    assert _writes(tournament)[0] == 0


def test_the_page_read_of_a_tournament_with_a_board_writes_nothing(
    client, admin, make_tournament
):
    tournament = make_tournament()
    _generate(tournament, admin)
    _change_board(tournament, admin)
    before = _writes(tournament)

    client.get(_url(tournament))

    assert _writes(tournament) == before


def test_start_gate_shows_the_group_shortfall(client, admin, make_tournament):
    tournament = make_tournament(
        (GameFormat.ONE_V_ONE, EliminationMode.ROUND_ROBIN),
        playoff_game_format=GameFormat.ONE_V_ONE,
        playoff_elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        playoff_group_count=6,
        playoff_qualifiers_per_group=1,
        playoff_release_mode=PlayoffReleaseMode.MANUAL,
    )

    _generate(tournament, admin)

    html = client.get(_url(tournament)).get_data(as_text=True)

    assert 'The roster gives 4 groups instead of the configured 6.' in html
    assert 'Only 4 contestants qualify instead of the configured 6.' in html
    assert f'/tournaments/{tournament.id}/start"' in html
    assert 'data-lt-start-gate="blocked"' not in html


def test_start_gate_shows_no_shortfall_for_a_full_roster(
    client, make_tournament
):
    tournament = make_tournament(
        (GameFormat.ONE_V_ONE, EliminationMode.ROUND_ROBIN),
        playoff_game_format=GameFormat.ONE_V_ONE,
        playoff_elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        playoff_group_count=4,
        playoff_qualifiers_per_group=1,
        playoff_release_mode=PlayoffReleaseMode.MANUAL,
    )

    html = client.get(_url(tournament)).get_data(as_text=True)

    assert 'instead of the configured' not in html
