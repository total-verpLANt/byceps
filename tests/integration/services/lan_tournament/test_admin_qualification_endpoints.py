"""
tests.integration.services.lan_tournament.test_admin_qualification_endpoints
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Drives the admin qualification page, the decision, release, un-release and
leaderboard-close routes and the FFA advance redirect through a real admin
app.
"""

from datetime import datetime, UTC
from io import BytesIO
from itertools import count
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from babel.messages.mofile import write_mo
from babel.messages.pofile import read_po
from babel.support import Translations
import flask_babel
import pytest
from sqlalchemy import delete, select

from byceps.database import db
from byceps.services.lan_tournament import (
    lan_tournament_view_helpers as helpers,
    tournament_log_service,
    tournament_match_service,
    tournament_qualification_domain_service as domain,
    tournament_qualification_repository,
    tournament_qualification_service,
    tournament_repository,
    tournament_score_service,
    tournament_seeding_repository,
    tournament_seeding_service,
    tournament_service,
)
from byceps.services.lan_tournament.dbmodels.seeding import (
    DbTournamentSeeding,
)
from byceps.services.lan_tournament.dbmodels.tournament_log_entry import (
    DbTournamentLogEntry,
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
JSON = {'Accept': 'application/json'}

PARTY_ID = PartyID('lan-party-admin-qualification-endpoints')

PRD_SENTENCE_DE = (
    'Qualifikation kann aufgrund eines Gleichstands nicht automatisch'
    ' bestimmt werden. Orgaentscheidung erforderlich.'
)

_counter = count(1)


@pytest.fixture(scope='module')
def party(make_party, make_brand):
    brand = make_brand('adminqualbrand', 'Admin Qualification Brand')
    return make_party(brand, PARTY_ID, 'LAN Party Admin Qualification')


@pytest.fixture(scope='module')
def users(make_user):
    return [make_user(f'AdminQualUser{i}') for i in range(8)]


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


@pytest.fixture(scope='module')
def client(make_client, admin_app, admin):
    return make_client(admin_app, user_id=admin.id)


@pytest.fixture(scope='module')
def viewer_client(make_client, admin_app, viewer):
    return make_client(admin_app, user_id=viewer.id)


@pytest.fixture(scope='module')
def german_translations():
    po_path = (
        Path(__file__).parents[4]
        / 'byceps/translations/de/LC_MESSAGES/messages.po'
    )
    with po_path.open('rb') as f:
        catalog = read_po(f, locale='de')
    buffer = BytesIO()
    write_mo(buffer, catalog)
    buffer.seek(0)
    return Translations(fp=buffer)


@pytest.fixture
def german(monkeypatch, german_translations):
    monkeypatch.setattr(
        flask_babel.Domain,
        'get_translations',
        lambda self: german_translations,
    )


@pytest.fixture
def make_tournament(party, users):
    created = []

    def _create(name, **kwargs):
        result = tournament_service.create_tournament(
            PARTY_ID,
            f'{name} {next(_counter)}',
            contestant_type=ContestantType.SOLO,
            **kwargs,
        )
        assert result.is_ok(), result.unwrap_err()
        tournament, _ = result.unwrap()
        created.append(tournament)
        return tournament

    def _join(tournament, count_):
        for user in users[:count_]:
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

    def _groups(
        *,
        participants=8,
        qualifiers_per_group=2,
        group_count=2,
        playoff_mode=EliminationMode.SINGLE_ELIMINATION,
        release_mode=PlayoffReleaseMode.MANUAL,
        started=True,
    ):
        tournament = _create(
            'Admin Qualification Groups',
            game_format=GameFormat.ONE_V_ONE,
            elimination_mode=EliminationMode.ROUND_ROBIN,
            tournament_status=TournamentStatus.REGISTRATION_CLOSED,
            playoff_game_format=GameFormat.ONE_V_ONE,
            playoff_elimination_mode=playoff_mode,
            playoff_group_count=group_count,
            playoff_qualifiers_per_group=qualifiers_per_group,
            playoff_release_mode=release_mode,
        )
        _join(tournament, participants)
        generated = tournament_match_service.generate_round_robin_bracket(
            tournament.id
        )
        assert generated.is_ok(), generated.unwrap_err()
        if started:
            started_result = tournament_service.change_status(
                tournament.id, TournamentStatus.ONGOING, users[0].id
            )
            assert started_result.is_ok(), started_result.unwrap_err()
        return tournament

    def _highscore(*, participants=5, qualifiers=4):
        tournament = _create(
            'Admin Qualification Highscore',
            game_format=GameFormat.HIGHSCORE,
            elimination_mode=EliminationMode.NONE,
            tournament_status=TournamentStatus.ONGOING,
            playoff_game_format=GameFormat.FREE_FOR_ALL,
            playoff_elimination_mode=EliminationMode.SINGLE_ELIMINATION,
            playoff_qualifier_count=qualifiers,
            playoff_release_mode=PlayoffReleaseMode.MANUAL,
            point_table=[5, 3, 2, 1],
            group_size_min=3,
            group_size_max=4,
            advancement_count=2,
            score_ordering=ScoreOrdering.HIGHER_IS_BETTER,
        )
        _join(tournament, participants)
        return tournament

    def _ffa(*, participants=8):
        tournament = _create(
            'Admin Qualification Ffa',
            game_format=GameFormat.FREE_FOR_ALL,
            elimination_mode=EliminationMode.SINGLE_ELIMINATION,
            tournament_status=TournamentStatus.REGISTRATION_CLOSED,
            max_players=16,
            group_size_min=2,
            group_size_max=4,
            advancement_count=2,
            point_table=[10, 6, 3, 1],
        )
        _join(tournament, participants)
        generated = tournament_match_service.generate_ffa_round(
            tournament.id, initiator_id=users[0].id
        )
        assert generated.is_ok(), generated.unwrap_err()
        started_result = tournament_service.change_status(
            tournament.id, TournamentStatus.ONGOING, users[0].id
        )
        assert started_result.is_ok(), started_result.unwrap_err()
        return tournament

    class Factory:
        groups = staticmethod(_groups)
        highscore = staticmethod(_highscore)
        ffa = staticmethod(_ffa)

    yield Factory
    db.session.rollback()
    for tournament in created:
        if tournament_repository.find_tournament(tournament.id) is not None:
            tournament_service.delete_tournament(tournament.id)


def _url(tournament, suffix=''):
    return f'{BASE_URL}/tournaments/{tournament.id}{suffix}'


def _groups_of(tournament):
    """Return the phase-1 matches of each group with their contestant ids."""
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


def _confirm(match, ids, scores, admin):
    result = tournament_match_service.admin_set_and_confirm_match(
        match.id, admin.id, dict(zip(ids, scores, strict=True))
    )
    assert result.is_ok(), result.unwrap_err()


def _play_groups(tournament, admin):
    """Play every group match; the lower ID wins by the group number + 1."""
    for group, matches in _groups_of(tournament).items():
        margin = group + 1
        for match, ids in matches:
            low, high = sorted(ids, key=str)
            scores = (margin, 0) if ids[0] == low else (0, margin)
            _confirm(match, ids, scores, admin)


def _play_all_draws(tournament, admin):
    for matches in _groups_of(tournament).values():
        for match, ids in matches:
            _confirm(match, ids, (1, 1), admin)


def _state(tournament):
    result = tournament_qualification_service.get_qualification(tournament.id)
    assert result.is_ok(), result.unwrap_err()
    return result.unwrap()


def _released_at(tournament):
    db.session.rollback()
    return tournament_repository.get_tournament(
        tournament.id
    ).playoff_released_at


def _phase_two(tournament):
    db.session.rollback()
    return [
        m
        for m in tournament_repository.get_matches_for_tournament(tournament.id)
        if m.phase == 2
    ]


def _decisions(tournament):
    db.session.rollback()
    return tournament_qualification_repository.get_decisions_for_tournament(
        tournament.id
    )


def _draft_version(tournament):
    board = tournament_seeding_service.ensure_playoff_draft(tournament.id)
    assert board.is_ok(), board.unwrap_err()
    return board.unwrap().version


def _decide_form(state, **overrides):
    block = next(b for b in state.blockers if b.scope == 'group:0')
    form = {
        'scope': 'group:0',
        'action': 'save',
        'reason': 'Played a tiebreak at the table.',
        'order': list(block.contestant_ids),
    }
    form.update(overrides)
    return form


# -------------------------------------------------------------------- #
# page


def test_qualification_page_renders(client, admin, make_tournament):
    tournament = make_tournament.groups()
    _play_groups(tournament, admin)
    version = _draft_version(tournament)

    response = client.get(_url(tournament, '/qualification'))

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'data-ranking="group:0"' in html
    assert 'data-ranking="group:1"' in html
    assert 'data-release-panel' in html
    assert '/qualification/release' in html
    assert f'name="version" value="{version}"' in html
    state = _state(tournament)
    assert state.ready
    names = helpers.contestant_names(tournament.id)
    for qualifier in state.qualifiers:
        assert names[qualifier.contestant_id] in html


def _playoff_draft(tournament):
    db.session.rollback()
    return tournament_seeding_repository.find_seeding(
        tournament.id, tournament_seeding_service.PLAYOFF_TARGET
    )


def _drawn_entries(tournament):
    db.session.rollback()
    return [
        e
        for e in tournament_log_service.get_entries_for_tournament(
            tournament.id
        )
        if e.event_type == 'seeding-drawn'
        and e.data.get('target') == tournament_seeding_service.PLAYOFF_TARGET
    ]


def test_confirming_the_last_result_creates_the_manual_draft(
    admin, make_tournament
):
    tournament = make_tournament.groups()
    assert _playoff_draft(tournament) is None

    _play_groups(tournament, admin)

    assert _state(tournament).ready
    assert _playoff_draft(tournament) is not None


def test_qualification_page_get_writes_no_draft(client, admin, make_tournament):
    tournament = make_tournament.groups()
    _play_groups(tournament, admin)
    db.session.execute(
        delete(DbTournamentSeeding).filter_by(tournament_id=tournament.id)
    )
    db.session.commit()
    assert _playoff_draft(tournament) is None

    response = client.get(_url(tournament, '/qualification'))

    assert response.status_code == 200
    assert 'name="version"' not in response.get_data(as_text=True)
    assert _playoff_draft(tournament) is None


def _drop_playoff_draft(tournament):
    db.session.execute(
        delete(DbTournamentSeeding).filter_by(tournament_id=tournament.id)
    )
    db.session.commit()
    assert _playoff_draft(tournament) is None


def test_missing_draft_offers_the_create_button(client, admin, make_tournament):
    tournament = make_tournament.groups()
    _play_groups(tournament, admin)
    _drop_playoff_draft(tournament)

    html = client.get(_url(tournament, '/qualification')).get_data(as_text=True)

    assert f'/tournaments/{tournament.id}/qualification/draft/create' in html
    assert 'data-lt-draft-create' in html
    assert 'name="version"' not in html
    assert _playoff_draft(tournament) is None


def test_create_draft_route_creates_the_draft(client, admin, make_tournament):
    tournament = make_tournament.groups()
    _play_groups(tournament, admin)
    _drop_playoff_draft(tournament)

    response = client.post(_url(tournament, '/qualification/draft/create'))

    assert response.status_code == 302
    assert response.headers['Location'].endswith('/qualification')
    assert _playoff_draft(tournament) is not None
    html = client.get(_url(tournament, '/qualification')).get_data(as_text=True)
    assert 'data-lt-draft-create' not in html
    assert 'name="version"' in html


def test_create_draft_route_logs_and_stores_the_acting_admin(
    client, admin, make_tournament
):
    tournament = make_tournament.groups()
    _play_groups(tournament, admin)
    _drop_playoff_draft(tournament)
    before = len(_drawn_entries(tournament))

    client.post(_url(tournament, '/qualification/draft/create'))

    drawn = _drawn_entries(tournament)
    assert len(drawn) == before + 1
    assert drawn[-1].initiator_id == admin.id
    assert _playoff_draft(tournament).updated_by == admin.id


def test_create_draft_route_refuses_a_qualification_that_is_not_ready(
    client, admin, make_tournament
):
    tournament = make_tournament.groups()
    assert not _state(tournament).ready

    response = client.post(
        _url(tournament, '/qualification/draft/create'), headers=JSON
    )

    assert response.status_code == 422
    assert _playoff_draft(tournament) is None


def test_ready_qualification_of_a_paused_tournament_offers_no_create_button(
    client, admin, make_tournament
):
    tournament = make_tournament.groups()
    _play_groups(tournament, admin)
    _drop_playoff_draft(tournament)
    paused = tournament_service.change_status(
        tournament.id, TournamentStatus.PAUSED, admin.id
    )
    assert paused.is_ok(), paused.unwrap_err()

    html = client.get(_url(tournament, '/qualification')).get_data(as_text=True)

    assert 'data-lt-draft-create' not in html


def test_qualification_page_without_playoffs_redirects(client, make_tournament):
    plain = make_tournament.ffa()

    response = client.get(_url(plain, '/qualification'))

    assert response.status_code == 302
    assert response.headers['Location'].endswith(f'/tournaments/{plain.id}')


def test_qualification_page_unknown_tournament_404(client):
    response = client.get(f'{BASE_URL}/tournaments/not-a-uuid/qualification')

    assert response.status_code == 404


def test_cut_tie_page_renders_the_prd_sentence_in_german(
    client, admin, make_tournament, german
):
    tournament = make_tournament.groups(
        participants=4, qualifiers_per_group=1, started=False
    )
    _play_all_draws(tournament, admin)

    response = client.get(_url(tournament, '/qualification'))

    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert 'data-blocker="cut"' in html
    assert PRD_SENTENCE_DE in html
    assert 'Qualification cannot be determined' not in html


# -------------------------------------------------------------------- #
# decisions


def test_decide_requires_reason(client, admin, make_tournament):
    tournament = make_tournament.groups(
        participants=4, qualifiers_per_group=1, started=False
    )
    _play_all_draws(tournament, admin)
    state = _state(tournament)

    for reason in ('', '   '):
        response = client.post(
            _url(tournament, '/qualification/decisions'),
            data=_decide_form(state, reason=reason),
            headers=JSON,
        )

        assert response.status_code == 422
        assert response.get_json()['error']
    assert not _decisions(tournament)


def test_decide_saves_and_withdraws(client, admin, make_tournament):
    tournament = make_tournament.groups(
        participants=4, qualifiers_per_group=1, started=False
    )
    _play_all_draws(tournament, admin)
    state = _state(tournament)
    form = _decide_form(state)

    saved = client.post(
        _url(tournament, '/qualification/decisions'), data=form, headers=JSON
    )

    assert saved.status_code == 200
    payload = saved.get_json()['qualification']
    (decision,) = payload['decisions']
    assert decision['scope'] == 'group:0'
    assert decision['status'] == 'applied'
    assert decision['withdraw_ids'] == form['order']
    assert decision['reason'] == form['reason']
    assert decision['decided_by'] == admin.screen_name
    assert _decisions(tournament)['group:0'].reason == form['reason']

    withdrawn = client.post(
        _url(tournament, '/qualification/decisions'),
        data={
            'scope': 'group:0',
            'action': 'withdraw',
            'reason': 'The judges changed their mind.',
            'order': form['order'],
        },
        headers=JSON,
    )

    assert withdrawn.status_code == 200
    assert 'group:0' not in _decisions(tournament)


def test_admin_decides_a_crossover_tie(client, admin, make_tournament):
    tournament = make_tournament.groups(
        participants=4, qualifiers_per_group=2, started=False
    )
    for matches in _groups_of(tournament).values():
        for match, ids in matches:
            low = min(ids, key=str)
            _confirm(match, ids, (1, 0) if ids[0] == low else (0, 1), admin)
    ties = [b for b in _state(tournament).blockers if b.scope == 'crossover']
    assert len(ties) == 2

    page = client.get(_url(tournament, '/qualification'))
    saved = client.post(
        _url(tournament, '/qualification/decisions'),
        data={
            'scope': 'crossover',
            'action': 'save',
            'reason': 'Seeded by the orga.',
            'order': list(ties[0].contestant_ids),
        },
        headers=JSON,
    )

    html = page.get_data(as_text=True)
    assert page.status_code == 200
    assert html.count('data-blocker="seeding"') == 2
    assert 'Playoff seeding' in html
    assert saved.status_code == 200
    (decision,) = saved.get_json()['qualification']['decisions']
    assert decision['scope'] == 'crossover'
    assert decision['scope_label'] == 'Playoff seeding'
    assert decision['status'] == 'applied'
    assert decision['places'] == '1\u20132'
    assert not _state(tournament).ready


def test_withdraw_without_reason_is_refused(client, admin, make_tournament):
    tournament = make_tournament.groups(
        participants=4, qualifiers_per_group=1, started=False
    )
    _play_all_draws(tournament, admin)
    form = _decide_form(_state(tournament))
    assert (
        client.post(
            _url(tournament, '/qualification/decisions'),
            data=form,
            headers=JSON,
        ).status_code
        == 200
    )

    response = client.post(
        _url(tournament, '/qualification/decisions'),
        data={'scope': 'group:0', 'action': 'withdraw', 'order': form['order']},
        headers=JSON,
    )

    assert response.status_code == 422
    assert 'group:0' in _decisions(tournament)


def _play_two_ties_in_group_zero(tournament, admin):
    """Group 0 ends with the ties {A,C} and {B,D}; group 1 is a clean chain."""
    groups = _groups_of(tournament)
    for match, ids in groups[1]:
        low, high = sorted(ids, key=str)
        _confirm(match, ids, (1, 0) if ids[0] == low else (0, 1), admin)
    members = sorted({i for _, ids in groups[0] for i in ids}, key=str)
    by_name = dict(zip('ABCD', members, strict=True))
    name_of = {i: n for n, i in by_name.items()}
    plan = {
        'AB': {'A': 1, 'B': 0},
        'AC': {'A': 1, 'C': 1},
        'AD': {'A': 1, 'D': 1},
        'BC': {'B': 1, 'C': 1},
        'BD': {'B': 1, 'D': 1},
        'CD': {'C': 1, 'D': 0},
    }
    for match, ids in groups[0]:
        scores = plan[''.join(sorted(name_of[i] for i in ids))]
        _confirm(match, ids, [scores[name_of[i]] for i in ids], admin)


def test_withdraw_names_one_block_of_two(client, admin, make_tournament):
    tournament = make_tournament.groups(
        participants=8, qualifiers_per_group=3, started=False
    )
    _play_two_ties_in_group_zero(tournament, admin)
    ties = [b for b in _state(tournament).blockers if b.scope == 'group:0']
    assert len(ties) == 2
    for tie in ties:
        saved = client.post(
            _url(tournament, '/qualification/decisions'),
            data=_decide_form(
                _state(tournament),
                order=list(tie.contestant_ids),
                reason=f'Tie {tie.rank_from}',
            ),
            headers=JSON,
        )
        assert saved.status_code == 200, saved.get_json()
    assert len(_decisions(tournament)['group:0'].blocks) == 2
    keep, drop = (list(t.contestant_ids) for t in ties)

    withdrawn = client.post(
        _url(tournament, '/qualification/decisions'),
        data={
            'scope': 'group:0',
            'action': 'withdraw',
            'reason': 'Only this one.',
            'order': drop,
        },
        headers=JSON,
    )

    assert withdrawn.status_code == 200
    rows = withdrawn.get_json()['qualification']['decisions']
    assert [r['withdraw_ids'] for r in rows] == [keep]
    assert [
        b.contestant_ids for b in _decisions(tournament)['group:0'].blocks
    ] == [tuple(keep)]


def test_withdraw_naming_no_block_is_refused(client, admin, make_tournament):
    tournament = make_tournament.groups(
        participants=4, qualifiers_per_group=1, started=False
    )
    _play_all_draws(tournament, admin)
    form = _decide_form(_state(tournament))
    client.post(
        _url(tournament, '/qualification/decisions'), data=form, headers=JSON
    )

    response = client.post(
        _url(tournament, '/qualification/decisions'),
        data={'scope': 'group:0', 'action': 'withdraw', 'reason': 'Oops.'},
        headers=JSON,
    )

    assert response.status_code == 422
    assert 'group:0' in _decisions(tournament)


def test_decide_without_json_redirects_to_the_page(
    client, admin, make_tournament
):
    tournament = make_tournament.groups(
        participants=4, qualifiers_per_group=1, started=False
    )
    _play_all_draws(tournament, admin)
    state = _state(tournament)

    response = client.post(
        _url(tournament, '/qualification/decisions'),
        data=_decide_form(state, reason=''),
    )

    assert response.status_code == 302
    assert response.headers['Location'].endswith('/qualification')
    assert not _decisions(tournament)


def test_decide_rejects_an_unknown_action(client, admin, make_tournament):
    tournament = make_tournament.groups(
        participants=4, qualifiers_per_group=1, started=False
    )
    _play_all_draws(tournament, admin)
    state = _state(tournament)

    response = client.post(
        _url(tournament, '/qualification/decisions'),
        data=_decide_form(state, action='bogus'),
        headers=JSON,
    )

    assert response.status_code == 422
    assert not _decisions(tournament)


# -------------------------------------------------------------------- #
# release


def test_admin_qualification_json_success_uses_the_shared_names(
    client, admin, make_tournament, monkeypatch
):
    from unittest.mock import Mock

    from byceps.services.lan_tournament.blueprints.admin import views

    names = Mock(wraps=helpers.contestant_names)
    payload = Mock(wraps=views._qualification_payload)
    monkeypatch.setattr(views, 'contestant_names', names)
    monkeypatch.setattr(views, '_qualification_payload', payload)

    def post(tournament, suffix, data):
        names.reset_mock()
        payload.reset_mock()
        response = client.post(_url(tournament, suffix), data=data, headers=JSON)
        assert response.status_code == 200
        qualification = response.get_json()['qualification']
        assert qualification is not None
        assert {'source', 'ready', 'qualifiers', 'decisions'} <= qualification.keys()
        names.assert_called_once_with(tournament.id)
        assert len(payload.call_args.args) == 3
        assert payload.call_args.args[2]
        return qualification

    tied = make_tournament.groups(
        participants=4, qualifiers_per_group=1, started=False
    )
    _play_all_draws(tied, admin)
    assert post(
        tied, '/qualification/decisions', _decide_form(_state(tied))
    )['decisions']

    groups = make_tournament.groups()
    _play_groups(groups, admin)
    assert post(
        groups, '/qualification/release',
        {'version': str(_draft_version(groups))},
    )['release']['released_at']
    assert post(
        groups, '/qualification/unrelease', {'reason': 'Correct qualifiers'}
    )['release']['released_at'] is None

    leaderboard = make_tournament.highscore()
    assert tournament_score_service.close_leaderboard(
        leaderboard.id, initiator_id=admin.id
    ).is_ok()
    assert post(
        leaderboard, '/leaderboard/reopen', {'reason': 'Correct scores'}
    )['leaderboard_closed'] is False


def test_release_manual(client, admin, make_tournament):
    tournament = make_tournament.groups()
    _play_groups(tournament, admin)
    version = _draft_version(tournament)
    assert _released_at(tournament) is None

    response = client.post(
        _url(tournament, '/qualification/release'),
        data={'version': str(version)},
    )

    assert response.status_code == 302
    assert response.headers['Location'].endswith('/qualification')
    assert _released_at(tournament) is not None
    assert _phase_two(tournament)
    page = client.get(_url(tournament, '/qualification'))
    html = page.get_data(as_text=True)
    assert 'data-event="playoffs-released"' in html
    assert '/qualification/unrelease' in html
    assert '/qualification/release' not in html


def test_release_with_a_shortfall_flashes_and_logs(
    client, admin, make_tournament
):
    tournament = make_tournament.groups(
        participants=6,
        group_count=4,
        qualifiers_per_group=1,
        playoff_mode=EliminationMode.DOUBLE_ELIMINATION,
    )
    _play_groups(tournament, admin)
    assert len(_groups_of(tournament)) == 3
    version = _draft_version(tournament)

    response = client.post(
        _url(tournament, '/qualification/release'),
        data={'version': str(version)},
    )

    assert response.status_code == 302
    assert _released_at(tournament) is not None
    assert _phase_two(tournament)
    page = client.get(_url(tournament, '/qualification'))
    html = page.get_data(as_text=True)
    assert 'data-event="playoffs-shortfall"' in html
    assert 'data-event="playoffs-de-fallback"' in html
    assert html.count('data-release-notice') >= 1
    assert 'Only 3 of 4 playoff places are filled.' in html
    assert (
        tournament_repository.get_tournament(
            tournament.id
        ).playoff_elimination_mode
        is EliminationMode.SINGLE_ELIMINATION
    )


def test_release_version_conflict_409(client, admin, make_tournament):
    tournament = make_tournament.groups()
    _play_groups(tournament, admin)
    version = _draft_version(tournament)

    response = client.post(
        _url(tournament, '/qualification/release'),
        data={'version': str(version + 5)},
        headers=JSON,
    )

    assert response.status_code == 409
    assert response.get_json()['error']
    assert _released_at(tournament) is None
    assert not _phase_two(tournament)


def test_release_refused_while_a_tie_blocks(client, admin, make_tournament):
    tournament = make_tournament.groups(participants=4, qualifiers_per_group=1)
    _play_all_draws(tournament, admin)

    response = client.post(
        _url(tournament, '/qualification/release'),
        data={'version': '1'},
        headers=JSON,
    )

    assert response.status_code == 422
    assert _released_at(tournament) is None


def test_release_malformed_version_422(client, admin, make_tournament):
    tournament = make_tournament.groups()
    _play_groups(tournament, admin)

    response = client.post(
        _url(tournament, '/qualification/release'),
        data={'version': 'x'},
        headers=JSON,
    )

    assert response.status_code == 422
    assert _released_at(tournament) is None


def test_unrelease_takes_the_release_back(client, admin, make_tournament):
    tournament = make_tournament.groups()
    _play_groups(tournament, admin)
    released = tournament_qualification_service.release_playoffs(
        tournament.id,
        expected_version=_draft_version(tournament),
        initiator_id=admin.id,
    )
    assert released.is_ok(), released.unwrap_err()

    missing_reason = client.post(
        _url(tournament, '/qualification/unrelease'),
        data={'reason': ' '},
        headers=JSON,
    )
    assert missing_reason.status_code == 422
    assert _released_at(tournament) is not None

    response = client.post(
        _url(tournament, '/qualification/unrelease'),
        data={'reason': 'A group result was entered wrong.'},
        headers=JSON,
    )

    assert response.status_code == 200
    assert _released_at(tournament) is None
    assert not _phase_two(tournament)


def test_unrelease_accepts_a_two_line_reason(client, admin, make_tournament):
    tournament = make_tournament.groups()
    _play_groups(tournament, admin)
    released = tournament_qualification_service.release_playoffs(
        tournament.id,
        expected_version=_draft_version(tournament),
        initiator_id=admin.id,
    )
    assert released.is_ok(), released.unwrap_err()

    response = client.post(
        _url(tournament, '/qualification/unrelease'),
        data={'reason': 'Wrong result in group A.\r\nEntered twice.'},
        headers=JSON,
    )

    assert response.status_code == 200, response.get_data(as_text=True)
    assert _released_at(tournament) is None
    entry = db.session.scalars(
        select(DbTournamentLogEntry).filter_by(
            tournament_id=tournament.id, event_type='playoffs-unreleased'
        )
    ).one()
    assert entry.data['reason'] == 'Wrong result in group A.\nEntered twice.'


def test_unrelease_of_a_cancelled_tournament_is_refused(
    client, admin, make_tournament
):
    tournament = make_tournament.groups()
    _play_groups(tournament, admin)
    released = tournament_qualification_service.release_playoffs(
        tournament.id,
        expected_version=_draft_version(tournament),
        initiator_id=admin.id,
    )
    assert released.is_ok(), released.unwrap_err()
    cancelled = tournament_service.change_status(
        tournament.id, TournamentStatus.CANCELLED, admin.id
    )
    assert cancelled.is_ok(), cancelled.unwrap_err()

    response = client.post(
        _url(tournament, '/qualification/unrelease'),
        data={'reason': 'Too late.'},
        headers=JSON,
    )

    assert response.status_code == 422
    assert response.get_json()['error']
    assert _released_at(tournament) is not None
    page = client.get(_url(tournament, '/qualification'))
    assert '/qualification/unrelease' not in page.get_data(as_text=True)


def test_unrelease_after_result_refused(client, admin, make_tournament):
    tournament = make_tournament.groups()
    _play_groups(tournament, admin)
    released = tournament_qualification_service.release_playoffs(
        tournament.id,
        expected_version=_draft_version(tournament),
        initiator_id=admin.id,
    )
    assert released.is_ok(), released.unwrap_err()
    contestants = tournament_repository.get_contestants_for_tournament(
        tournament.id
    )
    played = next(
        (m, [c.participant_id for c in contestants[m.id]])
        for m in _phase_two(tournament)
        if len(contestants.get(m.id, [])) == 2
    )
    _confirm(played[0], played[1], (3, 1), admin)

    response = client.post(
        _url(tournament, '/qualification/unrelease'),
        data={'reason': 'Too late, but trying.'},
        headers=JSON,
    )

    assert response.status_code == 422
    assert response.get_json()['error']
    assert _released_at(tournament) is not None
    assert _phase_two(tournament)
    page = client.get(_url(tournament, '/qualification'))
    assert '/qualification/unrelease' not in page.get_data(as_text=True)


# -------------------------------------------------------------------- #
# playoff draft


def _swap_playoff(version, p=0, q=2):
    return {
        'target': 'playoff',
        'version': str(version),
        'action': 'swap',
        'p': str(p),
        'q': str(q),
    }


def test_playoff_draft_is_editable_before_the_release(
    client, admin, make_tournament
):
    tournament = make_tournament.groups()
    _play_groups(tournament, admin)
    version = _draft_version(tournament)

    page = client.get(_url(tournament, '/seeding?target=playoff'))
    assert page.status_code == 200

    response = client.post(
        _url(tournament, '/seeding/actions'),
        data=_swap_playoff(version),
        headers=JSON,
    )

    assert response.status_code == 200, response.get_json()
    board = response.get_json()['board']
    assert board['target'] == 'playoff'
    assert board['version'] == version + 1
    stored = tournament_seeding_repository.find_seeding(
        tournament.id, 'playoff'
    )
    assert stored.version == version + 1


def test_playoff_draft_is_locked_after_the_first_result(
    client, admin, make_tournament
):
    tournament = make_tournament.groups()
    _play_groups(tournament, admin)
    released = tournament_qualification_service.release_playoffs(
        tournament.id,
        expected_version=_draft_version(tournament),
        initiator_id=admin.id,
    )
    assert released.is_ok(), released.unwrap_err()
    version = tournament_seeding_repository.find_seeding(
        tournament.id, 'playoff'
    ).version
    contestants = tournament_repository.get_contestants_for_tournament(
        tournament.id
    )
    match, ids = next(
        (m, [c.participant_id for c in contestants[m.id]])
        for m in _phase_two(tournament)
        if len(contestants.get(m.id, [])) == 2
    )
    _confirm(match, ids, (3, 1), admin)

    response = client.post(
        _url(tournament, '/seeding/actions'),
        data=_swap_playoff(version),
        headers=JSON,
    )

    assert response.status_code == 422
    db.session.rollback()
    stored = tournament_seeding_repository.find_seeding(
        tournament.id, 'playoff'
    )
    assert stored.version == version


# -------------------------------------------------------------------- #
# leaderboard


def _submit(tournament, participant, score):
    result = tournament_score_service.submit_score(
        tournament.id,
        score,
        participant_id=TournamentParticipantID(str(participant.id)),
    )
    assert result.is_ok(), result.unwrap_err()


def test_leaderboard_close_route(client, admin, make_tournament):
    tournament = make_tournament.highscore()
    participants = tournament_repository.get_participants_for_tournament(
        tournament.id
    )
    for participant, score in zip(
        participants, (50, 40, 30, 20, 10), strict=True
    ):
        _submit(tournament, participant, score)
    before = client.get(_url(tournament, '/qualification'))
    assert '/leaderboard/close' in before.get_data(as_text=True)

    response = client.post(_url(tournament, '/leaderboard/close'), headers=JSON)

    assert response.status_code == 200
    db.session.rollback()
    closed = tournament_repository.get_tournament(tournament.id)
    assert isinstance(closed.leaderboard_closed_at, datetime)
    payload = response.get_json()['qualification']
    assert payload['source'] == 'leaderboard'
    assert payload['leaderboard_closed'] is True
    assert payload['ready'] is True
    after = client.get(_url(tournament, '/qualification'))
    assert '/leaderboard/close' not in after.get_data(as_text=True)

    again = client.post(_url(tournament, '/leaderboard/close'), headers=JSON)

    assert again.status_code == 422


def test_leaderboard_reopen_route(client, admin, make_tournament):
    tournament = make_tournament.highscore()
    assert tournament_score_service.close_leaderboard(
        tournament.id, initiator_id=admin.id
    ).is_ok()
    before = client.get(_url(tournament, '/qualification')).get_data(as_text=True)
    assert '/leaderboard/reopen' in before
    response = client.post(
        _url(tournament, '/leaderboard/reopen'),
        data={'reason': 'Correct scores'},
        headers=JSON,
    )
    assert response.status_code == 200
    assert response.get_json()['qualification']['leaderboard_closed'] is False
    page = client.get(_url(tournament, '/qualification')).get_data(as_text=True)
    assert '/leaderboard/reopen' not in page
    assert '/leaderboard/close' in page


def test_reopen_form_shows_only_when_closed_and_unreleased(
    client, admin, make_tournament
):
    tournament = make_tournament.highscore()
    page = _url(tournament, '/qualification')
    assert '/leaderboard/reopen' not in client.get(page).get_data(as_text=True)
    assert tournament_score_service.close_leaderboard(
        tournament.id, initiator_id=admin.id
    ).is_ok()
    assert '/leaderboard/reopen' in client.get(page).get_data(as_text=True)
    assert tournament_service.change_status(
        tournament.id, TournamentStatus.PAUSED, admin.id
    ).is_ok()
    assert '/leaderboard/reopen' in client.get(page).get_data(as_text=True)
    response = client.post(
        _url(tournament, '/leaderboard/reopen'),
        data={'reason': 'Paused correction'},
        headers=JSON,
    )
    assert response.status_code == 200
    tournament_repository.set_leaderboard_closed(
        tournament.id, datetime.now(UTC).replace(tzinfo=None)
    )
    tournament_repository.set_playoff_release(
        tournament.id,
        released_at=datetime.now(UTC).replace(tzinfo=None),
        released_by=admin.id,
    )
    db.session.commit()
    assert '/leaderboard/reopen' not in client.get(page).get_data(as_text=True)


def test_leaderboard_reopen_requires_administrate_permission(
    viewer_client, make_tournament
):
    tournament = make_tournament.highscore()
    response = viewer_client.post(
        _url(tournament, '/leaderboard/reopen'),
        data={'reason': 'Correct scores'},
        headers=JSON,
    )
    assert response.status_code == 403


def test_close_button_shows_only_while_the_tournament_is_ongoing(
    client, admin, make_tournament
):
    tournament = make_tournament.highscore()
    participants = tournament_repository.get_participants_for_tournament(
        tournament.id
    )
    for participant, score in zip(
        participants, (50, 40, 30, 20, 10), strict=True
    ):
        _submit(tournament, participant, score)
    close_url = f'/tournaments/{tournament.id}/leaderboard/close'
    page = _url(tournament, '/qualification')
    assert close_url in client.get(page).get_data(as_text=True)

    paused = tournament_service.change_status(
        tournament.id, TournamentStatus.PAUSED, admin.id
    )
    assert paused.is_ok(), paused.unwrap_err()
    assert close_url not in client.get(page).get_data(as_text=True)

    resumed = tournament_service.change_status(
        tournament.id, TournamentStatus.ONGOING, admin.id
    )
    assert resumed.is_ok(), resumed.unwrap_err()
    assert close_url in client.get(page).get_data(as_text=True)


def test_leaderboard_close_refused_without_playoffs(
    client, admin, make_tournament
):
    tournament = make_tournament.groups()

    response = client.post(_url(tournament, '/leaderboard/close'), headers=JSON)

    assert response.status_code == 422
    db.session.rollback()
    found = tournament_repository.get_tournament(tournament.id)
    assert found.leaderboard_closed_at is None


# -------------------------------------------------------------------- #
# FFA advance


def _play_lobby(match, admin):
    ids = [
        str(c.participant_id)
        for c in tournament_match_service.get_contestants_for_match(match.id)
    ]
    placed = tournament_match_service.set_ffa_placements(
        match.id, {cid: i + 1 for i, cid in enumerate(ids)}
    )
    assert placed.is_ok(), placed.unwrap_err()
    confirmed = tournament_match_service.confirm_ffa_match(match.id, admin.id)
    assert confirmed.is_ok(), confirmed.unwrap_err()


def test_ffa_advance_route_redirects_to_draft(client, admin, make_tournament):
    tournament = make_tournament.ffa()
    for match in tournament_repository.get_matches_for_round(tournament.id, 0):
        _play_lobby(match, admin)

    response = client.post(_url(tournament, '/advance_ffa_round'))

    assert response.status_code == 302
    location = urlparse(response.headers['Location'])
    assert location.path.endswith(f'/tournaments/{tournament.id}/seeding')
    assert parse_qs(location.query) == {'target': ['ffa:SE:1']}
    db.session.rollback()
    assert (
        tournament_seeding_repository.find_seeding(tournament.id, 'ffa:SE:1')
        is not None
    )
    assert not tournament_repository.get_matches_for_round(tournament.id, 1)


def test_ffa_advance_route_while_the_round_is_open_flashes(
    client, make_tournament
):
    tournament = make_tournament.ffa()

    response = client.post(_url(tournament, '/advance_ffa_round'))

    assert response.status_code == 302
    assert response.headers['Location'].endswith(
        f'/tournaments/{tournament.id}/bracket'
    )
    db.session.rollback()
    assert (
        tournament_seeding_repository.find_seeding(tournament.id, 'ffa:SE:1')
        is None
    )


# -------------------------------------------------------------------- #
# permission


def test_qualification_requires_administrate_permission(
    viewer_client, admin, make_tournament
):
    tournament = make_tournament.groups(
        participants=4, qualifiers_per_group=1, started=False
    )
    _play_all_draws(tournament, admin)
    state = _state(tournament)

    assert (
        viewer_client.get(_url(tournament, '/qualification')).status_code == 403
    )
    posts = {
        '/qualification/decisions': _decide_form(state),
        '/qualification/release': {'version': '1'},
        '/qualification/unrelease': {'reason': 'because'},
        '/qualification/draft/create': {},
        '/leaderboard/close': {},
        '/advance_ffa_round': {},
    }
    for suffix, data in posts.items():
        response = viewer_client.post(_url(tournament, suffix), data=data)
        assert response.status_code == 403, suffix
    assert not _decisions(tournament)


# -------------------------------------------------------------------- #
# serialization


def _block(kind, scope='group:0', ids=('a', 'b'), *, decided=False):
    return domain.TieBlock(
        scope=scope,
        contestant_ids=ids,
        rank_from=2,
        rank_to=3,
        decided=decided,
        kind=kind,
    )


def _serialized(blocks):
    ranking = domain.Ranking(
        scope='group:0',
        entries=(),
        ties=tuple(blocks),
        open_matches=0,
    )
    state = tournament_qualification_service.QualificationState(
        tournament_id='t',
        source='groups',
        rankings=(ranking,),
        blockers=tuple(
            b
            for b in blocks
            if b.kind is not domain.TieKind.HARMLESS and not b.decided
        ),
        open_match_count=0,
        total_match_count=1,
        ready=False,
        qualifiers=None,
        released_at=None,
        released_by=None,
        release_mode=PlayoffReleaseMode.MANUAL,
        auto_release_suspended=False,
        can_unrelease=False,
    )
    strings = {
        'tie_cut': 'CUT TEXT',
        'tie_seeding': 'SEEDING TEXT',
        'tie_winner': 'WINNER TEXT',
        'tie_harmless': 'HARMLESS TEXT',
        'scope_group': 'Group %(letter)s',
    }
    return helpers.serialize_qualification(
        state, {'a': 'Alice', 'b': 'Bob'}, strings
    )


@pytest.mark.parametrize(
    ('kind', 'text'),
    [
        (domain.TieKind.CUT, 'CUT TEXT'),
        (domain.TieKind.SEEDING, 'SEEDING TEXT'),
        (domain.TieKind.WINNER, 'WINNER TEXT'),
    ],
)
def test_serialize_maps_every_blocker_kind_to_its_text(kind, text):
    payload = _serialized([_block(kind)])

    (blocker,) = payload['blockers']
    assert blocker['text'] == text
    assert blocker['kind'] == kind.value
    assert blocker['blocking'] is True
    assert blocker['places'] == '2–3'
    assert [c['name'] for c in blocker['contestants']] == ['Alice', 'Bob']
    assert blocker['scope_label'] == 'Group A'


def test_serialize_keeps_a_harmless_tie_out_of_the_blockers():
    payload = _serialized([_block(domain.TieKind.HARMLESS)])

    assert payload['blockers'] == []
    (tie,) = payload['rankings'][0]['ties']
    assert tie['blocking'] is False
    assert tie['text'] == 'HARMLESS TEXT'


# -------------------------------------------------------------------- #
# embedded playoff draft


def _draft_url(tournament):
    return _url(tournament, '/qualification/draft')


def _draft_board(tournament):
    board = tournament_seeding_service.get_board(tournament.id, 'playoff')
    assert board.is_ok(), board.unwrap_err()
    return board.unwrap()


def _make_clash(client, tournament):
    """Swap so that one first-round match pairs two of one group."""
    board = _draft_board(tournament)
    by_label = {label: cid for cid, label in board.origin_labels.items()}
    layout = list(board.state.layout)
    first = layout.index(by_label['A1'])
    second = layout.index(by_label['A2'])
    response = client.post(
        _draft_url(tournament),
        data=_swap_playoff(board.version, second, first ^ 1),
        headers=JSON,
    )
    assert response.status_code == 200, response.get_json()
    return response.get_json()['board']


def test_qualification_page_embeds_the_playoff_draft(
    client, admin, make_tournament
):
    tournament = make_tournament.groups()
    _play_groups(tournament, admin)
    _draft_version(tournament)

    html = client.get(_url(tournament, '/qualification')).get_data(as_text=True)

    assert 'data-lt-playoff-draft' in html
    assert html.count('data-lt-seed-root') == 1
    assert (
        f'data-action-url="/lan-tournaments/tournaments/{tournament.id}'
        '/qualification/draft"'
    ) in html
    assert 'data-lt-playoff-version' in html
    assert html.count('class="lt-seed-org"') == 4
    assert 'is-same' not in html


def test_embedded_draft_flags_a_same_group_pairing(
    client, admin, make_tournament
):
    tournament = make_tournament.groups()
    _play_groups(tournament, admin)
    _draft_version(tournament)
    _make_clash(client, tournament)

    html = client.get(_url(tournament, '/qualification')).get_data(as_text=True)

    assert 'lt-seed-match is-same' in html
    assert 'are both from group A.' in html
    assert 'name="action" value="separate"' in html


def test_draft_route_separates_by_json(client, admin, make_tournament):
    tournament = make_tournament.groups()
    _play_groups(tournament, admin)
    _draft_version(tournament)
    clashing = _make_clash(client, tournament)
    assert any(m['same_group'] for m in clashing['layout']['matches'])

    response = client.post(
        _draft_url(tournament),
        data={
            'target': 'playoff',
            'version': str(clashing['version']),
            'action': 'separate',
        },
        headers=JSON,
    )

    assert response.status_code == 200, response.get_json()
    board = response.get_json()['board']
    assert board['version'] == clashing['version'] + 1
    assert not any(m['same_group'] for m in board['layout']['matches'])
    db.session.rollback()
    events = [
        e.event_type
        for e in tournament_log_service.get_entries_for_tournament(
            tournament.id
        )
    ]
    assert events.count('seeding-separated') == 1


def _flip_top_pair(tournament, admin, margin=5):
    """Reverse the result between the first and second place of group 0."""
    entries = next(
        r for r in _state(tournament).rankings if r.scope == 'group:0'
    ).entries
    pair = {entries[0].contestant_id, entries[1].contestant_id}
    match, ids = next(
        (m, i)
        for m, i in _groups_of(tournament)[0]
        if {str(x) for x in i} == pair
    )
    scores = tuple(
        margin if str(i) == entries[1].contestant_id else 0 for i in ids
    )
    tournament_match_service.unconfirm_match(match.id, admin.id).unwrap()
    _confirm(match, ids, scores, admin)


def test_draft_route_reprefills_a_stale_draft(client, admin, make_tournament):
    tournament = make_tournament.groups()
    _play_groups(tournament, admin)
    _draft_version(tournament)
    clashing = _make_clash(client, tournament)
    _flip_top_pair(tournament, admin)
    stale = _draft_board(tournament)
    assert stale.stale_ranks
    assert stale.version == clashing['version']

    response = client.post(
        _draft_url(tournament),
        data={
            'target': 'playoff',
            'version': str(stale.version),
            'action': 'reprefill',
        },
        headers=JSON,
    )

    assert response.status_code == 200, response.get_json()
    board = response.get_json()['board']
    assert board['stale_ranks'] is False
    assert board['stale'] is False
    assert board['version'] == stale.version + 1
    assert not any(m['same_group'] for m in board['layout']['matches'])
    fresh = _draft_board(tournament)
    assert fresh.state.seed_list == tuple(
        q.contestant_id for q in _state(tournament).seed_order
    )
    db.session.rollback()
    entries = [
        e
        for e in tournament_log_service.get_entries_for_tournament(
            tournament.id
        )
        if e.event_type == 'seeding-reprefilled'
    ]
    assert [e.initiator_id for e in entries] == [admin.id]


def test_draft_route_reprefill_with_a_stale_version_is_409(
    client, admin, make_tournament
):
    tournament = make_tournament.groups()
    _play_groups(tournament, admin)
    version = _draft_version(tournament)

    response = client.post(
        _draft_url(tournament),
        data={
            'target': 'playoff',
            'version': str(version - 1),
            'action': 'reprefill',
        },
        headers=JSON,
    )

    assert response.status_code == 409


def test_draft_route_separate_with_a_stale_version_is_409(
    client, admin, make_tournament
):
    tournament = make_tournament.groups()
    _play_groups(tournament, admin)
    _draft_version(tournament)
    clashing = _make_clash(client, tournament)

    response = client.post(
        _draft_url(tournament),
        data={
            'target': 'playoff',
            'version': str(clashing['version'] - 1),
            'action': 'separate',
        },
        headers=JSON,
    )

    assert response.status_code == 409
    assert _draft_board(tournament).version == clashing['version']


def test_draft_route_separate_without_a_clash_is_422(
    client, admin, make_tournament
):
    tournament = make_tournament.groups()
    _play_groups(tournament, admin)
    version = _draft_version(tournament)

    response = client.post(
        _draft_url(tournament),
        data={'version': str(version), 'action': 'separate'},
        headers=JSON,
    )

    assert response.status_code == 422
    assert 'same group' in response.get_json()['error']
    assert _draft_board(tournament).version == version


def test_draft_route_without_json_goes_back_to_the_qualification_page(
    client, admin, make_tournament
):
    tournament = make_tournament.groups()
    _play_groups(tournament, admin)
    _draft_version(tournament)
    clashing = _make_clash(client, tournament)

    response = client.post(
        _draft_url(tournament),
        data={'version': str(clashing['version']), 'action': 'separate'},
    )

    assert response.status_code == 302
    assert response.headers['Location'].endswith('/qualification')
    html = client.get(response.headers['Location']).get_data(as_text=True)
    assert 'Separated: ' in html
    assert ' \u2194 ' in html
    assert 'is-same' not in html


def test_draft_route_ignores_a_foreign_target(client, admin, make_tournament):
    tournament = make_tournament.groups()
    _play_groups(tournament, admin)
    version = _draft_version(tournament)

    response = client.post(
        _draft_url(tournament),
        data={**_swap_playoff(version), 'target': 'initial'},
        headers=JSON,
    )

    assert response.status_code == 200
    assert response.get_json()['board']['target'] == 'playoff'


def test_draft_route_requires_administrate_permission(
    viewer_client, admin, make_tournament
):
    tournament = make_tournament.groups()
    _play_groups(tournament, admin)
    version = _draft_version(tournament)

    response = viewer_client.post(
        _draft_url(tournament), data=_swap_playoff(version), headers=JSON
    )

    assert response.status_code == 403
    assert _draft_board(tournament).version == version


def test_draft_route_is_refused_after_the_first_playoff_result(
    client, admin, make_tournament
):
    tournament = make_tournament.groups()
    _play_groups(tournament, admin)
    released = tournament_qualification_service.release_playoffs(
        tournament.id,
        expected_version=_draft_version(tournament),
        initiator_id=admin.id,
    )
    assert released.is_ok(), released.unwrap_err()
    version = _draft_board(tournament).version
    contestants = tournament_repository.get_contestants_for_tournament(
        tournament.id
    )
    match, ids = next(
        (m, [c.participant_id for c in contestants[m.id]])
        for m in _phase_two(tournament)
        if len(contestants.get(m.id, [])) == 2
    )
    _confirm(match, ids, (3, 1), admin)

    response = client.post(
        _draft_url(tournament),
        data={'version': str(version), 'action': 'separate'},
        headers=JSON,
    )

    assert response.status_code == 422
    assert _draft_board(tournament).version == version


# -------------------------------------------------------------------- #
# lock notice on the match page


def _group_match(tournament):
    matches = tournament_repository.get_matches_for_tournament(tournament.id)
    return next(m for m in matches if m.phase == 1)


def test_match_page_shows_the_first_lock_wording_after_the_release(
    client, admin, make_tournament
):
    tournament = make_tournament.groups()
    _play_groups(tournament, admin)
    released = tournament_qualification_service.release_playoffs(
        tournament.id,
        expected_version=_draft_version(tournament),
        initiator_id=admin.id,
    )
    assert released.is_ok(), released.unwrap_err()

    html = client.get(
        f'{BASE_URL}/matches/{_group_match(tournament).id}'
    ).get_data(as_text=True)

    assert 'Take the release back first.' in html
    assert 'locked for good' not in html


def test_match_page_shows_the_final_lock_wording_after_a_playoff_result(
    client, admin, make_tournament
):
    tournament = make_tournament.groups()
    _play_groups(tournament, admin)
    released = tournament_qualification_service.release_playoffs(
        tournament.id,
        expected_version=_draft_version(tournament),
        initiator_id=admin.id,
    )
    assert released.is_ok(), released.unwrap_err()
    contestants = tournament_repository.get_contestants_for_tournament(
        tournament.id
    )
    match, ids = next(
        (m, [c.participant_id for c in contestants[m.id]])
        for m in _phase_two(tournament)
        if len(contestants.get(m.id, [])) == 2
    )
    _confirm(match, ids, (3, 1), admin)

    html = client.get(
        f'{BASE_URL}/matches/{_group_match(tournament).id}'
    ).get_data(as_text=True)

    assert 'locked for good' in html
    assert 'the release can no longer be undone' in html
    assert 'Take the release back first.' not in html
