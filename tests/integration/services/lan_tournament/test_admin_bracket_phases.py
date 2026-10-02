"""
tests.integration.services.lan_tournament.test_admin_bracket_phases
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Drives the admin bracket page of tournaments with a playoff phase: the
phase-1 section first, then the phase-2 section in its own format. Also the
FFA advance forms of the double-elimination page.
"""

from datetime import datetime, UTC
from itertools import count
import re

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

from tests.helpers import http_client, log_in_user
from tests.integration.services.lan_tournament.test_ffa_waiting_winner import (  # noqa: F401
    engine_admin,  # noqa: F811
    engine_party,
    engine_players,
    lobbies,
    make_engine,  # noqa: F811
    play,
)


BASE_URL = 'http://admin.acmecon.test/lan-tournaments'

PARTY_ID = PartyID('lan-party-admin-bracket-phases')

_counter = count(1)


@pytest.fixture(scope='module')
def party(make_party, make_brand):
    brand = make_brand('adminbracketbrand', 'Admin Bracket Phases Brand')
    return make_party(brand, PARTY_ID, 'LAN Party Admin Bracket Phases')


@pytest.fixture(scope='module')
def users(make_user):
    return [make_user(f'AdminBracketUser{i}') for i in range(8)]


@pytest.fixture(scope='module')
def admin(make_admin):
    user = make_admin(
        {'admin.access', 'lan_tournament.administrate', 'lan_tournament.view'}
    )
    log_in_user(user.id)
    return user


@pytest.fixture(scope='module')
def viewer(make_admin):
    user = make_admin({'admin.access', 'lan_tournament.view'})
    log_in_user(user.id)
    return user


@pytest.fixture
def make_groups(party, users):
    created = []

    def _make(*, release_mode=PlayoffReleaseMode.MANUAL):
        result = tournament_service.create_tournament(
            party.id,
            f'Admin Bracket Groups {next(_counter)}',
            contestant_type=ContestantType.SOLO,
            game_format=GameFormat.ONE_V_ONE,
            elimination_mode=EliminationMode.ROUND_ROBIN,
            tournament_status=TournamentStatus.REGISTRATION_CLOSED,
            playoff_game_format=GameFormat.ONE_V_ONE,
            playoff_elimination_mode=EliminationMode.SINGLE_ELIMINATION,
            playoff_group_count=2,
            playoff_qualifiers_per_group=2,
            playoff_release_mode=release_mode,
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

    def _make(game_format, elimination_mode, **kwargs):
        result = tournament_service.create_tournament(
            party.id,
            f'Admin Bracket Plain {next(_counter)}',
            contestant_type=ContestantType.SOLO,
            game_format=game_format,
            elimination_mode=elimination_mode,
            tournament_status=TournamentStatus.REGISTRATION_CLOSED,
            **kwargs,
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


@pytest.fixture
def make_highscore(party, users):
    created = []

    def _make(*, playoffs):
        kwargs = {}
        if playoffs:
            kwargs = dict(
                playoff_game_format=GameFormat.FREE_FOR_ALL,
                playoff_elimination_mode=EliminationMode.SINGLE_ELIMINATION,
                playoff_qualifier_count=4,
                playoff_release_mode=PlayoffReleaseMode.MANUAL,
            )
        result = tournament_service.create_tournament(
            party.id,
            f'Admin Bracket Highscore {next(_counter)}',
            contestant_type=ContestantType.SOLO,
            game_format=GameFormat.HIGHSCORE,
            elimination_mode=EliminationMode.NONE,
            tournament_status=TournamentStatus.ONGOING,
            point_table=[5, 3, 2, 1],
            group_size_min=3,
            group_size_max=4,
            advancement_count=2,
            **kwargs,
        )
        assert result.is_ok(), result.unwrap_err()
        tournament, _ = result.unwrap()
        created.append(tournament)
        return tournament

    yield _make
    db.session.rollback()
    for tournament in created:
        if tournament_repository.find_tournament(tournament.id) is not None:
            tournament_service.delete_tournament(tournament.id)


def _join(tournament, users):
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


def _groups_of(tournament):
    matches = tournament_repository.get_matches_for_tournament(tournament.id)
    contestants = tournament_repository.get_contestants_for_tournament(
        tournament.id
    )
    groups: dict[int, list] = {}
    for match in matches:
        if match.phase != 1:
            continue
        ids = [c.participant_id for c in contestants[match.id]]
        groups.setdefault(match.group_order, []).append((match, ids))
    return groups


def _confirm(match, ids, scores, initiator):
    result = tournament_match_service.admin_set_and_confirm_match(
        match.id, initiator.id, dict(zip(ids, scores, strict=True))
    )
    assert result.is_ok(), result.unwrap_err()


def _play_groups(tournament, initiator):
    """Play every group match; the lower ID wins by the group number + 1."""
    for group, matches in _groups_of(tournament).items():
        margin = group + 1
        for match, ids in matches:
            low, _high = sorted(ids, key=str)
            scores = (margin, 0) if ids[0] == low else (0, margin)
            _confirm(match, ids, scores, initiator)


def _play_all_draws(tournament, initiator):
    for matches in _groups_of(tournament).values():
        for match, ids in matches:
            _confirm(match, ids, (1, 1), initiator)


def _release(tournament, initiator):
    board = tournament_seeding_service.ensure_playoff_draft(tournament.id)
    assert board.is_ok(), board.unwrap_err()
    released = tournament_qualification_service.release_playoffs(
        tournament.id,
        expected_version=board.unwrap().version,
        initiator_id=initiator.id,
    )
    assert released.is_ok(), released.unwrap_err()


def _phase_two(tournament):
    db.session.rollback()
    return [
        m
        for m in tournament_repository.get_matches_for_tournament(tournament.id)
        if m.phase == 2
    ]


def _get(app, user, path):
    with http_client(app, user_id=user.id) as client:
        response = client.get(f'{BASE_URL}{path}')
    assert response.status_code == 200
    return response.get_data(as_text=True)


def _bracket(app, user, tournament):
    return _get(app, user, f'/tournaments/{tournament.id}/bracket')


def _round_lobbies(tournament, round_number):
    return sorted(
        tournament_repository.get_matches_for_round(
            tournament.id, round_number
        ),
        key=lambda m: m.group_order or 0,
    )


def _match_count(html):
    return len(re.findall(r"class='bracket-match'", html))


def test_groups_bracket_shows_the_group_cards_then_the_playoff_bracket(
    admin_app, admin, make_groups
):
    tournament = make_groups()
    _play_groups(tournament, admin)
    _release(tournament, admin)

    html = _bracket(admin_app, admin, tournament)

    assert 'data-lt-phase="1"' in html
    assert html.index('data-lt-phase="1"') < html.index('data-lt-phase="2"')
    assert html.count('data-lt-ranking=') == 2
    assert _match_count(html) == len(_phase_two(tournament)) > 0
    assert "<table class='rr-standings'" not in html


def test_groups_bracket_waits_for_the_release(admin_app, admin, make_groups):
    tournament = make_groups()

    html = _bracket(admin_app, admin, tournament)

    assert 'data-lt-wait="groups"' in html
    assert _match_count(html) == 0


def test_groups_bracket_draws_the_cut_line_by_rank(
    admin_app, admin, make_groups
):
    played = make_groups()
    _play_groups(played, admin)
    _release(played, admin)
    drawn = make_groups()
    _play_all_draws(drawn, admin)

    played_html = _bracket(admin_app, admin, played)
    drawn_html = _bracket(admin_app, admin, drawn)

    assert played_html.count('class="lt-seed-cut"') == 2
    assert drawn_html.count('data-lt-ranking=') == 2
    assert drawn_html.count('class="lt-seed-cut"') == 0


def test_highscore_bracket_runs_the_ffa_phase_two(
    admin_app,
    admin,
    make_engine,  # noqa: F811
    engine_admin,  # noqa: F811
):
    tournament = make_engine('highscore', double=False, size=8)
    first_round = _round_lobbies(tournament, 0)
    assert first_round, 'phase 2 not released'
    for match in first_round:
        play(match, engine_admin)

    html = _bracket(admin_app, admin, tournament)

    assert f'/tournaments/{tournament.id}/advance_ffa_round' in html


def test_highscore_de_bracket_offers_the_pool_advances(
    admin_app,
    admin,
    make_engine,  # noqa: F811
    engine_admin,  # noqa: F811
):
    tournament = make_engine('highscore', double=True, size=8)
    for match in lobbies(tournament, Bracket.WINNERS, 0):
        play(match, engine_admin)

    html = _bracket(admin_app, admin, tournament)

    assert "name='pool' value='WB'" in html


def test_ffa_de_bracket_offers_the_wb_advance_when_both_pools_are_confirmed(
    admin_app,
    admin,
    make_engine,  # noqa: F811
    engine_admin,  # noqa: F811
):
    tournament = make_engine('plain', double=True, size=16, minimum=3, cut=2)
    for match in lobbies(tournament, Bracket.WINNERS, 0):
        play(match, engine_admin)
    tournament_match_service.advance_ffa_round(
        tournament.id, pool=Bracket.WINNERS, initiator_id=engine_admin.id
    ).unwrap()
    for match in lobbies(tournament, Bracket.WINNERS, 1):
        play(match, engine_admin)
    for match in lobbies(tournament, Bracket.LOSERS, 0):
        play(match, engine_admin)

    html = _bracket(admin_app, admin, tournament)

    assert "name='pool' value='WB'" in html


def test_highscore_de_bracket_wires_the_gate_into_the_gf_offer(
    admin_app,
    admin,
    make_engine,  # noqa: F811
    engine_admin,  # noqa: F811
):
    tournament = make_engine('highscore', double=True, size=4)
    gf_action = f'/tournaments/{tournament.id}/generate_ffa_grand_final'
    before = _bracket(admin_app, admin, tournament)
    for match in lobbies(tournament, Bracket.WINNERS, 0):
        play(match, engine_admin)
    tournament_match_service.advance_ffa_round(
        tournament.id, pool=Bracket.WINNERS, initiator_id=engine_admin.id
    ).unwrap()
    db.session.rollback()
    ready = _bracket(admin_app, admin, tournament)

    assert gf_action not in before
    assert gf_action in ready
    assert "name='pool' value='WB'" not in ready


def test_plain_round_robin_bracket_is_unchanged(admin_app, admin, make_plain):
    tournament = make_plain(GameFormat.ONE_V_ONE, EliminationMode.ROUND_ROBIN)
    generated = tournament_match_service.generate_round_robin_bracket(
        tournament.id
    )
    assert generated.is_ok(), generated.unwrap_err()

    html = _bracket(admin_app, admin, tournament)

    assert "<table class='rr-standings'" in html
    assert 'data-lt-phase' not in html


def test_plain_ffa_bracket_is_unchanged(
    admin_app,
    admin,
    make_engine,  # noqa: F811
    engine_admin,  # noqa: F811
):
    tournament = make_engine('plain', double=False, size=8)
    for match in _round_lobbies(tournament, 0):
        play(match, engine_admin)

    html = _bracket(admin_app, admin, tournament)

    assert f'/tournaments/{tournament.id}/advance_ffa_round' in html
    assert 'data-lt-phase' not in html


def test_admin_view_links_the_playoffs_of_a_highscore_tournament(
    admin_app, admin, make_highscore
):
    with_playoffs = make_highscore(playoffs=True)
    without_playoffs = make_highscore(playoffs=False)

    linked = _get(admin_app, admin, f'/tournaments/{with_playoffs.id}')
    plain = _get(admin_app, admin, f'/tournaments/{without_playoffs.id}')

    assert f'/tournaments/{with_playoffs.id}/bracket' in linked
    assert f'/tournaments/{without_playoffs.id}/bracket' not in plain


def test_view_holder_sees_no_seeds_or_forms_on_the_phase_sections(
    admin_app, viewer, admin, make_groups
):
    tournament = make_groups()
    _play_groups(tournament, admin)
    _release(tournament, admin)

    html = _bracket(admin_app, viewer, tournament)

    assert 'data-lt-phase="1"' in html
    assert '/qualification' not in html.split('data-lt-phase="1"', 1)[1]
