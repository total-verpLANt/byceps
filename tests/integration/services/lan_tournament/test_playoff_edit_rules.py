"""
tests.integration.services.lan_tournament.test_playoff_edit_rules
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The playoff settings stay editable until the release, and are locked
after it in every status.
"""

from datetime import datetime, UTC
from itertools import count
import re
from uuid import UUID

import pytest

from byceps.database import db
from byceps.services.lan_tournament import (
    tournament_match_service,
    tournament_repository,
    tournament_seeding_service,
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
from byceps.services.party.models import PartyID
from byceps.util.uuid import generate_uuid7

from tests.helpers import log_in_user


BASE_URL = 'http://admin.acmecon.test/lan-tournaments'

PARTY_ID = PartyID('lan-party-2026-playoff-edit-rules')

_counter = count(1)


@pytest.fixture(scope='module')
def party(make_party, make_brand):
    brand = make_brand('playoffeditrulesbrand', 'Playoff Edit Rules Brand')
    return make_party(brand, PARTY_ID, 'LAN Party 2026 Playoff Edit Rules')


@pytest.fixture(scope='module')
def users(make_user):
    return [make_user(f'PlayoffEditRulesUser{i}') for i in range(8)]


@pytest.fixture(scope='module')
def admin(make_admin):
    user = make_admin(
        {
            'admin.access',
            'lan_tournament.administrate',
            'lan_tournament.update',
            'lan_tournament.view',
        }
    )
    log_in_user(user.id)
    return user


@pytest.fixture(scope='module')
def client(make_client, admin_app, admin):
    return make_client(admin_app, user_id=admin.id)


@pytest.fixture
def running(party, users, admin):
    created = []

    def _make(mode=PlayoffReleaseMode.MANUAL):
        result = tournament_service.create_tournament(
            PARTY_ID,
            f'Playoff Edit Rules Tournament {next(_counter)}',
            contestant_type=ContestantType.SOLO,
            game_format=GameFormat.ONE_V_ONE,
            elimination_mode=EliminationMode.ROUND_ROBIN,
            tournament_status=TournamentStatus.REGISTRATION_CLOSED,
            playoff_game_format=GameFormat.ONE_V_ONE,
            playoff_elimination_mode=EliminationMode.SINGLE_ELIMINATION,
            playoff_group_count=2,
            playoff_qualifiers_per_group=2,
            playoff_release_mode=mode,
        )
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
        board = tournament_seeding_service.get_board(
            tournament.id, initiator_id=admin.id
        ).unwrap()
        tournament_seeding_service.generate_from_seeding(
            tournament.id, expected_version=board.version, initiator_id=admin.id
        ).unwrap()
        tournament_service.change_status(
            tournament.id, TournamentStatus.ONGOING, admin.id
        ).unwrap()
        return tournament

    yield _make
    db.session.rollback()


def _play_groups(tournament, admin):
    """Let the lower ID win every group match.

    The margin differs per group, so the groups do not tie exactly and
    no seeding tie remains.
    """
    for match in tournament_repository.get_matches_for_tournament(
        tournament.id
    ):
        members = sorted(
            str(c.participant_id)
            for c in tournament_repository.get_contestants_for_match(match.id)
        )
        margin = (match.group_order or 0) + 1
        tournament_match_service.admin_set_and_confirm_match(
            match.id, admin.id, {UUID(members[0]): margin, UUID(members[1]): 0}
        ).unwrap()


def _found(tournament):
    db.session.rollback()
    return tournament_repository.get_tournament(tournament.id)


def _post_form(**playoff):
    """Return what a browser posts for an ongoing tournament."""
    return {'description': 'Edited', 'image_url': '', 'ruleset': '', **playoff}


def _tag(html, name):
    match = re.search(rf'<(?:input|select)[^>]*name="{name}"[^>]*>', html)
    assert match is not None, name
    return match.group(0)


def test_an_admin_edits_the_cut_of_a_running_tournament(client, admin, running):
    tournament = running()

    response = client.post(
        f'{BASE_URL}/tournaments/{tournament.id}',
        data=_post_form(
            playoff_qualifiers_per_group='1',
            playoff_elimination_mode='SINGLE_ELIMINATION',
            playoff_release_mode='MANUAL',
        ),
    )

    assert response.status_code == 302
    found = _found(tournament)
    assert found.playoff_qualifiers_per_group == 1
    assert found.playoff_group_count == 2
    assert found.description == 'Edited'


def test_switching_to_automatic_with_a_ready_qualification_releases(
    client, admin, running
):
    tournament = running()
    _play_groups(tournament, admin)
    assert _found(tournament).playoff_released_at is None

    response = client.post(
        f'{BASE_URL}/tournaments/{tournament.id}',
        data=_post_form(
            playoff_qualifiers_per_group='2',
            playoff_elimination_mode='SINGLE_ELIMINATION',
            playoff_release_mode='AUTOMATIC',
        ),
    )

    assert response.status_code == 302
    assert _found(tournament).playoff_released_at is not None


def test_a_released_completed_tournament_refuses_a_playoff_edit(admin, running):
    tournament = running(PlayoffReleaseMode.AUTOMATIC)
    _play_groups(tournament, admin)
    assert _found(tournament).playoff_released_at is not None
    tournament_service.change_status(
        tournament.id, TournamentStatus.COMPLETED, admin.id
    ).unwrap()
    stored = _found(tournament)

    result = tournament_service.update_tournament(
        tournament.id,
        name=stored.name,
        contestant_type=stored.contestant_type,
        game_format=stored.game_format,
        elimination_mode=stored.elimination_mode,
        playoff_qualifiers_per_group=1,
    )

    assert result.is_err()
    assert result.unwrap_err() == tournament_service.PLAYOFF_RELEASED_EDIT_ERROR
    assert _found(tournament).playoff_qualifiers_per_group == 2


def test_the_update_form_of_a_running_tournament_offers_the_cut(
    client, running
):
    tournament = running()

    html = client.get(
        f'{BASE_URL}/tournaments/{tournament.id}/update'
    ).get_data(as_text=True)

    assert 'disabled' not in _tag(html, 'playoff_qualifiers_per_group')
    assert 'disabled' not in _tag(html, 'playoff_release_mode')
    assert 'disabled' in _tag(html, 'playoff_group_count')
    assert 'disabled' in _tag(html, 'playoff_enabled')


def test_a_post_cannot_change_the_cut_of_a_released_tournament(
    client, admin, running
):
    tournament = running(PlayoffReleaseMode.AUTOMATIC)
    _play_groups(tournament, admin)
    assert _found(tournament).playoff_released_at is not None

    response = client.post(
        f'{BASE_URL}/tournaments/{tournament.id}',
        data=_post_form(
            playoff_qualifiers_per_group='1',
            playoff_elimination_mode='SINGLE_ELIMINATION',
            playoff_release_mode='MANUAL',
        ),
    )

    assert response.status_code == 302
    found = _found(tournament)
    assert found.description == 'Edited'
    assert found.playoff_qualifiers_per_group == 2
    assert found.playoff_release_mode is PlayoffReleaseMode.AUTOMATIC


def test_the_update_form_of_a_released_tournament_locks_every_playoff_field(
    client, admin, running
):
    tournament = running(PlayoffReleaseMode.AUTOMATIC)
    _play_groups(tournament, admin)
    assert _found(tournament).playoff_released_at is not None

    html = client.get(
        f'{BASE_URL}/tournaments/{tournament.id}/update'
    ).get_data(as_text=True)

    for name in (
        'playoff_enabled',
        'playoff_group_count',
        'playoff_qualifiers_per_group',
        'playoff_elimination_mode',
        'playoff_release_mode',
    ):
        assert 'disabled' in _tag(html, name), name
