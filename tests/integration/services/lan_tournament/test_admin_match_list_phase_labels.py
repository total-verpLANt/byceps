"""
tests.integration.services.lan_tournament.test_admin_match_list_phase_labels
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Drives the admin match list: a tournament with playoffs labels each match
with its group or playoff round, a plain tournament shows no label.
"""

from itertools import count
import re

import pytest

from byceps.database import db
from byceps.services.lan_tournament import (
    tournament_match_service,
    tournament_repository,
    tournament_service,
)
from byceps.services.lan_tournament.models import ContestantType
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.playoff import PlayoffReleaseMode
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.party.models import PartyID
from byceps.util.uuid import generate_uuid7

from tests.helpers import log_in_user
from tests.integration.services.lan_tournament.test_admin_bracket_phases import (
    _get,
    _join,
    _play_groups,
    _release,
)


_counter = count(1)


@pytest.fixture(scope='module')
def party(make_party, make_brand):
    suffix = generate_uuid7().hex[:12]
    brand = make_brand(f'matchlistbrand{suffix}', 'Match List Phase Brand')
    return make_party(
        brand, PartyID(f'lan-party-match-list-{suffix}'), 'LAN Party Match List'
    )


@pytest.fixture(scope='module')
def users(make_user):
    suffix = generate_uuid7().hex[:12]
    return [make_user(f'MatchListUser{suffix}{i}') for i in range(8)]


@pytest.fixture(scope='module')
def admin(make_admin):
    user = make_admin(
        {'admin.access', 'lan_tournament.administrate', 'lan_tournament.view'}
    )
    log_in_user(user.id)
    return user


@pytest.fixture
def make_groups(party, users):
    created = []

    def _make():
        result = tournament_service.create_tournament(
            party.id,
            f'Match List Groups {next(_counter)}',
            contestant_type=ContestantType.SOLO,
            game_format=GameFormat.ONE_V_ONE,
            elimination_mode=EliminationMode.ROUND_ROBIN,
            tournament_status=TournamentStatus.REGISTRATION_CLOSED,
            playoff_game_format=GameFormat.ONE_V_ONE,
            playoff_elimination_mode=EliminationMode.SINGLE_ELIMINATION,
            playoff_group_count=2,
            playoff_qualifiers_per_group=2,
            playoff_release_mode=PlayoffReleaseMode.MANUAL,
        )
        assert result.is_ok(), result.unwrap_err()
        tournament, _ = result.unwrap()
        created.append(tournament)
        _join(tournament, users)
        generated = tournament_match_service.generate_round_robin_bracket(
            tournament.id
        )
        assert generated.is_ok(), generated.unwrap_err()
        started = tournament_service.change_status(
            tournament.id, TournamentStatus.ONGOING, users[0].id
        )
        assert started.is_ok(), started.unwrap_err()
        return tournament

    yield _make
    db.session.rollback()
    for tournament in created:
        if tournament_repository.find_tournament(tournament.id) is not None:
            tournament_service.delete_tournament(tournament.id)


@pytest.fixture
def make_plain(party, users):
    created = []

    def _make(game_format, elimination_mode):
        result = tournament_service.create_tournament(
            party.id,
            f'Match List Plain {next(_counter)}',
            contestant_type=ContestantType.SOLO,
            game_format=game_format,
            elimination_mode=elimination_mode,
            tournament_status=TournamentStatus.REGISTRATION_CLOSED,
        )
        assert result.is_ok(), result.unwrap_err()
        tournament, _ = result.unwrap()
        created.append(tournament)
        _join(tournament, users)
        return tournament

    yield _make
    db.session.rollback()
    for tournament in created:
        if tournament_repository.find_tournament(tournament.id) is not None:
            tournament_service.delete_tournament(tournament.id)


def _list(app, user, tournament):
    return _get(app, user, f'/tournaments/{tournament.id}/matches?only=all')


def _row_of(html, match):
    rows = re.findall(r'<tr>.*?</tr>', html, re.S)
    own = [row for row in rows if f'/matches/{match.id}' in row]
    assert len(own) == 1
    return own[0]


def test_released_rr_se_list_labels_groups_and_playoffs(
    admin_app,
    admin,
    make_groups,
):
    tournament = make_groups()
    _play_groups(tournament, admin)
    _release(tournament, admin)

    matches = tournament_match_service.get_matches_for_tournament_ordered(
        tournament.id
    )
    group_matches = [m for m in matches if m.phase == 1]
    playoff_matches = [m for m in matches if m.phase == 2]
    assert group_matches
    assert playoff_matches

    html = _list(admin_app, admin, tournament)

    for match in group_matches:
        letter = chr(ord('A') + (match.group_order or 0))
        row = _row_of(html, match)
        assert f'<small class="dimmed">Group {letter}</small>' in row
    for match in playoff_matches:
        row = _row_of(html, match)
        assert '<small class="dimmed">Playoffs · ' in row


def test_plain_se_list_has_no_phase_labels(
    admin_app,
    admin,
    make_plain,
):
    tournament = make_plain(
        GameFormat.ONE_V_ONE, EliminationMode.SINGLE_ELIMINATION
    )
    generated = tournament_match_service.generate_single_elimination_bracket(
        tournament.id
    )
    assert generated.is_ok(), generated.unwrap_err()

    html = _list(admin_app, admin, tournament)

    assert "class='button button--compact'" in html
    assert '<small class="dimmed">' not in html
    assert 'Group A' not in html
    assert 'Playoffs' not in html
