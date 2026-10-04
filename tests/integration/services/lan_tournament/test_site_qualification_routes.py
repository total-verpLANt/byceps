"""
tests.integration.services.lan_tournament.test_site_qualification_routes
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Drives the qualification page, the decision, release, un-release and
leaderboard-close routes and the FFA advance redirect of scoped orgas
through a real site app.
"""

from datetime import datetime, UTC
from itertools import count
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import delete

from byceps.database import db
from byceps.services.lan_tournament import (
    lan_tournament_view_helpers,
    tournament_match_service,
    tournament_orga_service,
    tournament_qualification_repository,
    tournament_qualification_service,
    tournament_repository,
    tournament_score_service,
    tournament_log_service,
    tournament_seeding_repository,
    tournament_seeding_service,
    tournament_service,
)
from byceps.services.lan_tournament.dbmodels.seeding import (
    DbTournamentSeeding,
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

from tests.helpers import http_client, log_in_user


BASE_URL = 'http://www.acmecon.test/lan-tournaments'
JSON = {'Accept': 'application/json'}

OTHER_PARTY_ID = PartyID('lan-party-site-qualification-other')

_counter = count(1)


@pytest.fixture(scope='module')
def other_party(make_party, make_brand):
    brand = make_brand('sitequalotherbrand', 'Site Qualification Other Brand')
    return make_party(brand, OTHER_PARTY_ID, 'LAN Party Site Qualification')


@pytest.fixture(scope='module')
def players(make_user):
    return [make_user(f'SiteQualPlayer{i}') for i in range(8)]


@pytest.fixture(scope='module')
def orga(make_user):
    user = make_user('SiteQualOrga')
    log_in_user(user.id)
    return user


@pytest.fixture(scope='module')
def bystander(make_user):
    user = make_user('SiteQualBystander')
    log_in_user(user.id)
    return user


@pytest.fixture
def make_tournament(party, players, orga):
    created = []

    def _create(party_id, name, **kwargs):
        result = tournament_service.create_tournament(
            party_id,
            f'{name} {next(_counter)}',
            contestant_type=ContestantType.SOLO,
            **kwargs,
        )
        assert result.is_ok(), result.unwrap_err()
        tournament, _ = result.unwrap()
        created.append(tournament)
        return tournament

    def _join(tournament, count_):
        for user in players[:count_]:
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

    def _make_orga(tournament, orga_user):
        if orga_user is not None:
            tournament_orga_service.assign_orga(
                tournament.id, orga_user.id, orga_user.id
            ).unwrap()

    def _groups(
        *,
        party_id=None,
        participants=8,
        qualifiers_per_group=2,
        release_mode=PlayoffReleaseMode.MANUAL,
        started=True,
        orga_user=orga,
    ):
        tournament = _create(
            party_id or party.id,
            'Site Qualification Groups',
            game_format=GameFormat.ONE_V_ONE,
            elimination_mode=EliminationMode.ROUND_ROBIN,
            tournament_status=TournamentStatus.REGISTRATION_CLOSED,
            playoff_game_format=GameFormat.ONE_V_ONE,
            playoff_elimination_mode=EliminationMode.SINGLE_ELIMINATION,
            playoff_group_count=2,
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
                tournament.id, TournamentStatus.ONGOING, players[0].id
            )
            assert started_result.is_ok(), started_result.unwrap_err()
        _make_orga(tournament, orga_user)
        return tournament

    def _highscore(*, participants=5, qualifiers=4):
        tournament = _create(
            party.id,
            'Site Qualification Highscore',
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
        _make_orga(tournament, orga)
        return tournament

    def _ffa(*, participants=8):
        tournament = _create(
            party.id,
            'Site Qualification Ffa',
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
            tournament.id, initiator_id=players[0].id
        )
        assert generated.is_ok(), generated.unwrap_err()
        started_result = tournament_service.change_status(
            tournament.id, TournamentStatus.ONGOING, players[0].id
        )
        assert started_result.is_ok(), started_result.unwrap_err()
        _make_orga(tournament, orga)
        return tournament

    def _plain_rr(*, participants=4):
        tournament = _create(
            party.id,
            'Site Plain Round Robin',
            game_format=GameFormat.ONE_V_ONE,
            elimination_mode=EliminationMode.ROUND_ROBIN,
            tournament_status=TournamentStatus.REGISTRATION_CLOSED,
        )
        _join(tournament, participants)
        generated = tournament_match_service.generate_round_robin_bracket(
            tournament.id
        )
        assert generated.is_ok(), generated.unwrap_err()
        started_result = tournament_service.change_status(
            tournament.id, TournamentStatus.ONGOING, players[0].id
        )
        assert started_result.is_ok(), started_result.unwrap_err()
        _make_orga(tournament, orga)
        return tournament

    class Factory:
        groups = staticmethod(_groups)
        plain_rr = staticmethod(_plain_rr)
        highscore = staticmethod(_highscore)
        ffa = staticmethod(_ffa)

    yield Factory
    db.session.rollback()
    for tournament in created:
        if tournament_repository.find_tournament(tournament.id) is not None:
            tournament_service.delete_tournament(tournament.id)


def _url(tournament, suffix=''):
    return f'{BASE_URL}/orga/tournaments/{tournament.id}{suffix}'


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
            low, high = sorted(ids, key=str)
            scores = (margin, 0) if ids[0] == low else (0, margin)
            _confirm(match, ids, scores, initiator)


def _play_all_draws(tournament, initiator):
    for matches in _groups_of(tournament).values():
        for match, ids in matches:
            _confirm(match, ids, (1, 1), initiator)


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


def _decide_form(state, scope, **overrides):
    block = next(b for b in state.blockers if b.scope == scope)
    form = {
        'scope': scope,
        'action': 'save',
        'reason': 'Played a tiebreak at the table.',
        'order': list(block.contestant_ids),
    }
    form.update(overrides)
    return form


def _decide_all(client, tournament):
    """Decide every open tie through the no-JS form; return the responses."""
    responses = []
    for scope in ('group:0', 'group:1'):
        responses.append(
            client.post(
                _url(tournament, '/qualification/decisions'),
                data=_decide_form(_state(tournament), scope),
            )
        )
    return responses


# -------------------------------------------------------------------- #
# page


def test_qualification_page_renders_for_a_scoped_orga(
    site_app, orga, make_tournament
):
    tournament = make_tournament.groups()
    _play_groups(tournament, orga)
    version = _draft_version(tournament)

    with http_client(site_app, user_id=orga.id) as client:
        response = client.get(_url(tournament, '/qualification'))

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'data-ranking="group:0"' in html
    assert 'data-ranking="group:1"' in html
    assert 'data-release-panel' in html
    assert f'/orga/tournaments/{tournament.id}/qualification/release' in html
    assert f'name="version" value="{version}"' in html


def test_qualification_page_get_writes_no_draft(
    site_app, orga, make_tournament
):
    tournament = make_tournament.groups()
    _play_groups(tournament, orga)
    db.session.execute(
        delete(DbTournamentSeeding).filter_by(tournament_id=tournament.id)
    )
    db.session.commit()
    assert _playoff_draft(tournament) is None

    with http_client(site_app, user_id=orga.id) as client:
        response = client.get(_url(tournament, '/qualification'))

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'name="version"' not in html
    assert 'data-lt-draft-create' in html
    assert _playoff_draft(tournament) is None


def test_create_draft_route_creates_the_draft(site_app, orga, make_tournament):
    tournament = make_tournament.groups()
    _play_groups(tournament, orga)
    db.session.execute(
        delete(DbTournamentSeeding).filter_by(tournament_id=tournament.id)
    )
    db.session.commit()
    assert _playoff_draft(tournament) is None

    with http_client(site_app, user_id=orga.id) as client:
        page = client.get(_url(tournament, '/qualification'))
        response = client.post(_url(tournament, '/qualification/draft/create'))
        after = client.get(_url(tournament, '/qualification'))

    assert (
        f'/orga/tournaments/{tournament.id}/qualification/draft/create'
        in page.get_data(as_text=True)
    )
    assert response.status_code == 302
    assert response.headers['Location'].endswith('/qualification')
    assert _playoff_draft(tournament) is not None
    html = after.get_data(as_text=True)
    assert 'data-lt-draft-create' not in html
    assert 'name="version"' in html


def test_create_draft_route_logs_and_stores_the_acting_orga(
    site_app, orga, make_tournament
):
    tournament = make_tournament.groups()
    _play_groups(tournament, orga)
    db.session.execute(
        delete(DbTournamentSeeding).filter_by(tournament_id=tournament.id)
    )
    db.session.commit()
    before = len(_drawn_entries(tournament))

    with http_client(site_app, user_id=orga.id) as client:
        client.post(_url(tournament, '/qualification/draft/create'))

    drawn = _drawn_entries(tournament)
    assert len(drawn) == before + 1
    assert drawn[-1].initiator_id == orga.id
    assert _playoff_draft(tournament).updated_by == orga.id


def test_create_draft_route_refuses_a_qualification_that_is_not_ready(
    site_app, orga, make_tournament
):
    tournament = make_tournament.groups()
    assert not _state(tournament).ready

    with http_client(site_app, user_id=orga.id) as client:
        response = client.post(
            _url(tournament, '/qualification/draft/create'), headers=JSON
        )

    assert response.status_code == 422
    assert _playoff_draft(tournament) is None


def test_qualification_page_without_playoffs_redirects(
    site_app, orga, make_tournament
):
    plain = make_tournament.ffa()

    with http_client(site_app, user_id=orga.id) as client:
        response = client.get(_url(plain, '/qualification'))

    assert response.status_code == 302
    assert response.headers['Location'].endswith(f'/{plain.id}')


# -------------------------------------------------------------------- #
# decide and release


def test_scoped_orga_can_decide_and_release(site_app, orga, make_tournament):
    tournament = make_tournament.groups(participants=4, qualifiers_per_group=1)
    _play_all_draws(tournament, orga)
    assert {b.scope for b in _state(tournament).blockers} == {
        'group:0',
        'group:1',
    }

    with http_client(site_app, user_id=orga.id) as client:
        page = client.get(_url(tournament, '/qualification'))
        html = page.get_data(as_text=True)
        assert page.status_code == 200
        assert html.count('data-qualification-decide') == 2
        assert 'name="version"' not in html
        assert (
            f'/orga/tournaments/{tournament.id}/qualification/decisions' in html
        )

        early = client.post(
            _url(tournament, '/qualification/release'),
            data={'version': '1'},
            headers=JSON,
        )
        decided = _decide_all(client, tournament)

        version = _draft_version(tournament)
        ready_page = client.get(_url(tournament, '/qualification'))
        released = client.post(
            _url(tournament, '/qualification/release'),
            data={'version': str(version)},
        )
        after = client.get(_url(tournament, '/qualification'))

    assert early.status_code == 422
    for response in decided:
        assert response.status_code == 302
        assert response.headers['Location'].endswith('/qualification')
    decisions = _decisions(tournament)
    assert set(decisions) == {'group:0', 'group:1'}
    assert {d.decided_by for d in decisions.values()} == {orga.id}
    assert f'name="version" value="{version}"' in ready_page.get_data(
        as_text=True
    )
    assert released.status_code == 302
    assert released.headers['Location'].endswith('/qualification')
    assert _released_at(tournament) is not None
    assert _phase_two(tournament)
    after_html = after.get_data(as_text=True)
    assert '/qualification/unrelease' in after_html
    assert 'data-lt-playoff-version' not in after_html


def test_scoped_orga_decides_a_crossover_tie(site_app, orga, make_tournament):
    tournament = make_tournament.groups(participants=4, qualifiers_per_group=2)
    for matches in _groups_of(tournament).values():
        for match, ids in matches:
            low = min(ids, key=str)
            _confirm(match, ids, (1, 0) if ids[0] == low else (0, 1), orga)
    state = _state(tournament)
    ties = [b for b in state.blockers if b.scope == 'crossover']
    assert not state.ready
    assert {b.scope for b in state.blockers} == {'crossover'}
    assert len(ties) == 2

    with http_client(site_app, user_id=orga.id) as client:
        page = client.get(_url(tournament, '/qualification'))
        decided = [
            client.post(
                _url(tournament, '/qualification/decisions'),
                data=_decide_form(
                    _state(tournament),
                    'crossover',
                    order=list(reversed(tie.contestant_ids)),
                    reason='Seeded by the orga.',
                ),
            )
            for tie in ties
        ]
        after = client.get(_url(tournament, '/qualification'))

    html = page.get_data(as_text=True)
    assert page.status_code == 200
    assert html.count('data-qualification-decide') == 2
    assert 'Playoff seeding' in html
    assert [r.status_code for r in decided] == [302, 302]
    stored = _decisions(tournament)['crossover']
    assert {b.contestant_ids for b in stored.blocks} == {
        tuple(reversed(t.contestant_ids)) for t in ties
    }
    assert {b.decided_by for b in stored.blocks} == {orga.id}
    state = _state(tournament)
    assert state.ready
    assert [q.contestant_id for q in state.seed_order] == [
        *reversed(ties[0].contestant_ids),
        *reversed(ties[1].contestant_ids),
    ]
    assert 'data-decision="crossover"' in after.get_data(as_text=True)


def test_decide_requires_a_reason(site_app, orga, make_tournament):
    tournament = make_tournament.groups(
        participants=4, qualifiers_per_group=1, started=False
    )
    _play_all_draws(tournament, orga)
    state = _state(tournament)

    with http_client(site_app, user_id=orga.id) as client:
        for reason in ('', '   '):
            response = client.post(
                _url(tournament, '/qualification/decisions'),
                data=_decide_form(state, 'group:0', reason=reason),
                headers=JSON,
            )

            assert response.status_code == 422
            assert response.get_json()['error']
        unknown = client.post(
            _url(tournament, '/qualification/decisions'),
            data=_decide_form(state, 'group:0', action='nope'),
            headers=JSON,
        )

    assert unknown.status_code == 422
    assert not _decisions(tournament)


def test_decide_accepts_a_two_line_reason(site_app, orga, make_tournament):
    tournament = make_tournament.groups(
        participants=4, qualifiers_per_group=1, started=False
    )
    _play_all_draws(tournament, orga)
    state = _state(tournament)

    with http_client(site_app, user_id=orga.id) as client:
        response = client.post(
            _url(tournament, '/qualification/decisions'),
            data=_decide_form(
                state,
                'group:0',
                reason='Played a tiebreak.\r\nThe orga watched.',
            ),
            headers=JSON,
        )

    assert response.status_code == 200, response.get_data(as_text=True)
    assert (
        _decisions(tournament)['group:0'].reason
        == 'Played a tiebreak.\nThe orga watched.'
    )


def test_scoped_orga_can_withdraw_a_decision_with_a_reason(
    site_app, orga, make_tournament
):
    tournament = make_tournament.groups(
        participants=4, qualifiers_per_group=1, started=False
    )
    _play_all_draws(tournament, orga)
    form = _decide_form(_state(tournament), 'group:0')
    withdraw = {
        'scope': 'group:0',
        'action': 'withdraw',
        'reason': 'The judges changed their mind.',
        'order': form['order'],
    }

    with http_client(site_app, user_id=orga.id) as client:
        saved = client.post(
            _url(tournament, '/qualification/decisions'),
            data=form,
            headers=JSON,
        )
        page = client.get(_url(tournament, '/qualification'))
        no_reason = client.post(
            _url(tournament, '/qualification/decisions'),
            data={
                'scope': 'group:0',
                'action': 'withdraw',
                'order': form['order'],
            },
            headers=JSON,
        )
        withdrawn = client.post(
            _url(tournament, '/qualification/decisions'),
            data=withdraw,
            headers=JSON,
        )

    assert saved.status_code == 200
    (decision,) = saved.get_json()['qualification']['decisions']
    assert decision['scope'] == 'group:0'
    assert decision['decided_by'] == orga.screen_name
    html = page.get_data(as_text=True)
    assert 'data-decision="group:0"' in html
    assert form['reason'] in html
    assert 'name="action" value="withdraw"' in html
    for cid in form['order']:
        assert f'name="order" value="{cid}"' in html
    assert no_reason.status_code == 422
    assert withdrawn.status_code == 200
    assert 'group:0' not in _decisions(tournament)


def _play_two_ties_in_group_zero(tournament, initiator):
    """Group 0 ends with the ties {A,C} and {B,D}; group 1 is a clean chain."""
    groups = _groups_of(tournament)
    for match, ids in groups[1]:
        low, high = sorted(ids, key=str)
        _confirm(match, ids, (1, 0) if ids[0] == low else (0, 1), initiator)
    members = sorted({i for _, ids in groups[0] for i in ids}, key=str)
    name_of = {i: n for n, i in zip('ABCD', members, strict=True)}
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
        _confirm(match, ids, [scores[name_of[i]] for i in ids], initiator)


def test_withdraw_names_one_block_of_two(site_app, orga, make_tournament):
    tournament = make_tournament.groups(
        participants=8, qualifiers_per_group=3, started=False
    )
    _play_two_ties_in_group_zero(tournament, orga)
    ties = [b for b in _state(tournament).blockers if b.scope == 'group:0']
    assert len(ties) == 2

    with http_client(site_app, user_id=orga.id) as client:
        for tie in ties:
            saved = client.post(
                _url(tournament, '/qualification/decisions'),
                data=_decide_form(
                    _state(tournament),
                    'group:0',
                    order=list(tie.contestant_ids),
                    reason=f'Tie {tie.rank_from}',
                ),
                headers=JSON,
            )
            assert saved.status_code == 200, saved.get_json()
        assert len(_decisions(tournament)['group:0'].blocks) == 2
        keep, drop = (list(t.contestant_ids) for t in ties)
        page = client.get(_url(tournament, '/qualification'))
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

    html = page.get_data(as_text=True)
    assert html.count('data-decision="group:0"') == 2
    assert withdrawn.status_code == 200
    rows = withdrawn.get_json()['qualification']['decisions']
    assert [r['withdraw_ids'] for r in rows] == [keep]
    blocks = _decisions(tournament)['group:0'].blocks
    assert [b.contestant_ids for b in blocks] == [tuple(keep)]


def test_decision_on_an_ffa_scope_returns_to_the_bracket(
    site_app, orga, make_tournament
):
    tournament = make_tournament.ffa()

    with http_client(site_app, user_id=orga.id) as client:
        response = client.post(
            _url(tournament, '/qualification/decisions'),
            data={
                'scope': 'ffa:SE:0',
                'action': 'save',
                'reason': 'Tiebreak at the table.',
                'order': ['x', 'y'],
            },
        )

    assert response.status_code == 302
    assert response.headers['Location'].endswith(f'/{tournament.id}/bracket')
    assert not _decisions(tournament)


def test_release_version_conflict_409(site_app, orga, make_tournament):
    tournament = make_tournament.groups()
    _play_groups(tournament, orga)
    version = _draft_version(tournament)

    with http_client(site_app, user_id=orga.id) as client:
        stale = client.post(
            _url(tournament, '/qualification/release'),
            data={'version': str(version + 5)},
            headers=JSON,
        )
        malformed = client.post(
            _url(tournament, '/qualification/release'),
            data={'version': 'x'},
            headers=JSON,
        )

    assert stale.status_code == 409
    assert stale.get_json()['error']
    assert malformed.status_code == 422
    assert _released_at(tournament) is None
    assert not _phase_two(tournament)


def test_scoped_orga_can_unrelease_with_a_reason(
    site_app, orga, make_tournament
):
    tournament = make_tournament.groups()
    _play_groups(tournament, orga)
    released = tournament_qualification_service.release_playoffs(
        tournament.id,
        expected_version=_draft_version(tournament),
        initiator_id=orga.id,
    )
    assert released.is_ok(), released.unwrap_err()

    with http_client(site_app, user_id=orga.id) as client:
        no_reason = client.post(
            _url(tournament, '/qualification/unrelease'),
            data={'reason': ' '},
            headers=JSON,
        )
        still_released = _released_at(tournament)
        response = client.post(
            _url(tournament, '/qualification/unrelease'),
            data={'reason': 'A group result was entered wrong.'},
        )

    assert no_reason.status_code == 422
    assert still_released is not None
    assert response.status_code == 302
    assert response.headers['Location'].endswith('/qualification')
    assert _released_at(tournament) is None
    assert not _phase_two(tournament)


def test_unrelease_after_a_playoff_result_is_refused(
    site_app, orga, make_tournament
):
    tournament = make_tournament.groups()
    _play_groups(tournament, orga)
    released = tournament_qualification_service.release_playoffs(
        tournament.id,
        expected_version=_draft_version(tournament),
        initiator_id=orga.id,
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
    _confirm(played[0], played[1], (3, 1), orga)

    with http_client(site_app, user_id=orga.id) as client:
        response = client.post(
            _url(tournament, '/qualification/unrelease'),
            data={'reason': 'Too late, but trying.'},
            headers=JSON,
        )
        page = client.get(_url(tournament, '/qualification'))

    assert response.status_code == 422
    assert _released_at(tournament) is not None
    assert '/qualification/unrelease' not in page.get_data(as_text=True)


# -------------------------------------------------------------------- #
# highscore


def _submit(tournament, participant, score):
    result = tournament_score_service.submit_score(
        tournament.id,
        score,
        participant_id=TournamentParticipantID(str(participant.id)),
    )
    assert result.is_ok(), result.unwrap_err()


def test_leaderboard_close_route(site_app, orga, make_tournament):
    tournament = make_tournament.highscore()
    participants = tournament_repository.get_participants_for_tournament(
        tournament.id
    )
    for participant, score in zip(
        participants, (50, 40, 30, 20, 10), strict=True
    ):
        _submit(tournament, participant, score)

    with http_client(site_app, user_id=orga.id) as client:
        before = client.get(_url(tournament, '/qualification'))
        response = client.post(
            _url(tournament, '/leaderboard/close'), headers=JSON
        )
        after = client.get(_url(tournament, '/qualification'))
        again = client.post(
            _url(tournament, '/leaderboard/close'), headers=JSON
        )

    assert f'/orga/tournaments/{tournament.id}/leaderboard/close' in (
        before.get_data(as_text=True)
    )
    assert response.status_code == 200
    payload = response.get_json()['qualification']
    assert payload['source'] == 'leaderboard'
    assert payload['leaderboard_closed'] is True
    assert '/leaderboard/close' not in after.get_data(as_text=True)
    assert again.status_code == 422
    db.session.rollback()
    closed = tournament_repository.get_tournament(tournament.id)
    assert isinstance(closed.leaderboard_closed_at, datetime)


def test_leaderboard_reopen_route(site_app, orga, make_tournament):
    tournament = make_tournament.highscore()
    assert tournament_score_service.close_leaderboard(
        tournament.id, initiator_id=orga.id
    ).is_ok()
    with http_client(site_app, user_id=orga.id) as client:
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


def test_close_button_shows_only_while_the_tournament_is_ongoing(
    site_app, orga, make_tournament
):
    tournament = make_tournament.highscore()
    participants = tournament_repository.get_participants_for_tournament(
        tournament.id
    )
    for participant, score in zip(
        participants, (50, 40, 30, 20, 10), strict=True
    ):
        _submit(tournament, participant, score)
    close_url = f'/orga/tournaments/{tournament.id}/leaderboard/close'

    def page():
        with http_client(site_app, user_id=orga.id) as client:
            return client.get(_url(tournament, '/qualification')).get_data(
                as_text=True
            )

    assert close_url in page()

    paused = tournament_service.change_status(
        tournament.id, TournamentStatus.PAUSED, orga.id
    )
    assert paused.is_ok(), paused.unwrap_err()
    assert close_url not in page()

    resumed = tournament_service.change_status(
        tournament.id, TournamentStatus.ONGOING, orga.id
    )
    assert resumed.is_ok(), resumed.unwrap_err()
    assert close_url in page()


def test_leaderboard_close_refused_without_playoffs(
    site_app, orga, make_tournament
):
    tournament = make_tournament.groups()

    with http_client(site_app, user_id=orga.id) as client:
        response = client.post(
            _url(tournament, '/leaderboard/close'), headers=JSON
        )

    assert response.status_code == 422
    db.session.rollback()
    assert (
        tournament_repository.get_tournament(
            tournament.id
        ).leaderboard_closed_at
        is None
    )


# -------------------------------------------------------------------- #
# FFA advance


def _play_lobby(match, initiator):
    ids = [
        str(c.participant_id)
        for c in tournament_match_service.get_contestants_for_match(match.id)
    ]
    placed = tournament_match_service.set_ffa_placements(
        match.id, {cid: i + 1 for i, cid in enumerate(ids)}
    )
    assert placed.is_ok(), placed.unwrap_err()
    confirmed = tournament_match_service.confirm_ffa_match(
        match.id, initiator.id
    )
    assert confirmed.is_ok(), confirmed.unwrap_err()


def test_site_orga_ffa_advance(site_app, orga, make_tournament):
    tournament = make_tournament.ffa()
    for match in tournament_repository.get_matches_for_round(tournament.id, 0):
        _play_lobby(match, orga)

    with http_client(site_app, user_id=orga.id) as client:
        response = client.post(_url(tournament, '/advance_ffa_round'))

    assert response.status_code == 302
    location = urlparse(response.headers['Location'])
    assert location.path.endswith(f'/orga/tournaments/{tournament.id}/seeding')
    assert parse_qs(location.query) == {'target': ['ffa:SE:1']}
    db.session.rollback()
    assert (
        tournament_seeding_repository.find_seeding(tournament.id, 'ffa:SE:1')
        is not None
    )
    assert not tournament_repository.get_matches_for_round(tournament.id, 1)


def test_site_orga_ffa_advance_while_the_round_is_open_flashes(
    site_app, orga, make_tournament
):
    tournament = make_tournament.ffa()

    with http_client(site_app, user_id=orga.id) as client:
        response = client.post(_url(tournament, '/advance_ffa_round'))

    assert response.status_code == 302
    assert response.headers['Location'].endswith(f'/{tournament.id}/bracket')
    db.session.rollback()
    assert (
        tournament_seeding_repository.find_seeding(tournament.id, 'ffa:SE:1')
        is None
    )


def test_site_ffa_advance_refuses_a_non_ffa_tournament(
    site_app, orga, make_tournament
):
    tournament = make_tournament.groups()

    with http_client(site_app, user_id=orga.id) as client:
        response = client.post(_url(tournament, '/advance_ffa_round'))

    assert response.status_code == 302
    assert response.headers['Location'].endswith(f'/{tournament.id}')
    db.session.rollback()
    assert (
        tournament_seeding_repository.find_seeding(tournament.id, 'ffa:SE:1')
        is None
    )


# -------------------------------------------------------------------- #
# authorization


def _all_requests(tournament, state_form):
    return [
        ('get', '/qualification', None),
        ('post', '/qualification/decisions', state_form),
        ('post', '/qualification/release', {'version': '1'}),
        ('post', '/qualification/unrelease', {'reason': 'because'}),
        (
            'post',
            '/qualification/draft',
            {'version': '1', 'action': 'separate'},
        ),
        ('post', '/qualification/draft/create', {}),
        ('post', '/leaderboard/close', {}),
        ('post', '/leaderboard/reopen', {'reason': 'because'}),
        ('post', '/advance_ffa_round', {}),
        ('post', '/generate_ffa_grand_final', {}),
    ]


def test_non_orga_forbidden(site_app, bystander, orga, make_tournament):
    tournament = make_tournament.groups(
        participants=4, qualifiers_per_group=1, started=False
    )
    _play_all_draws(tournament, orga)
    form = _decide_form(_state(tournament), 'group:0')

    with http_client(site_app, user_id=bystander.id) as client:
        for method, suffix, data in _all_requests(tournament, form):
            response = getattr(client, method)(
                _url(tournament, suffix), **({'data': data} if data else {})
            )
            assert response.status_code == 403, suffix

    assert not _decisions(tournament)
    assert _released_at(tournament) is None


def test_orga_of_another_tournament_forbidden(site_app, orga, make_tournament):
    tournament = make_tournament.groups(
        participants=4, qualifiers_per_group=1, started=False, orga_user=None
    )
    _play_all_draws(tournament, orga)
    form = _decide_form(_state(tournament), 'group:0')

    with http_client(site_app, user_id=orga.id) as client:
        for method, suffix, data in _all_requests(tournament, form):
            response = getattr(client, method)(
                _url(tournament, suffix), **({'data': data} if data else {})
            )
            assert response.status_code == 403, suffix

    assert not _decisions(tournament)


def test_anonymous_is_not_served(site_app, make_tournament):
    tournament = make_tournament.groups()

    with http_client(site_app) as client:
        get = client.get(_url(tournament, '/qualification'))
        post = client.post(
            _url(tournament, '/qualification/release'), data={'version': '1'}
        )

    assert get.status_code in (302, 401, 403)
    assert post.status_code in (302, 401, 403)
    assert b'data-ranking' not in get.data


def test_other_party_tournament_404(
    site_app, other_party, orga, make_tournament
):
    tournament = make_tournament.groups(
        party_id=other_party.id, participants=4, qualifiers_per_group=1
    )
    _play_all_draws(tournament, orga)
    form = _decide_form(_state(tournament), 'group:0')

    with http_client(site_app, user_id=orga.id) as client:
        for method, suffix, data in _all_requests(tournament, form):
            response = getattr(client, method)(
                _url(tournament, suffix), **({'data': data} if data else {})
            )
            assert response.status_code == 404, suffix

    assert not _decisions(tournament)
    assert _released_at(tournament) is None


def test_malformed_tournament_id_404(site_app, orga):
    with http_client(site_app, user_id=orga.id) as client:
        response = client.get(
            f'{BASE_URL}/orga/tournaments/not-a-uuid/qualification'
        )

    assert response.status_code == 404


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
        data={
            'version': str(board.version),
            'action': 'swap',
            'p': str(second),
            'q': str(first ^ 1),
        },
        headers=JSON,
    )
    assert response.status_code == 200, response.get_json()
    return response.get_json()['board']


def test_qualification_page_embeds_the_playoff_draft(
    site_app, orga, make_tournament
):
    tournament = make_tournament.groups()
    _play_groups(tournament, orga)
    _draft_version(tournament)

    with http_client(site_app, user_id=orga.id) as client:
        html = client.get(_url(tournament, '/qualification')).get_data(
            as_text=True
        )

    assert 'data-lt-playoff-draft' in html
    assert html.count('data-lt-seed-root') == 1
    assert (
        f'data-action-url="/lan-tournaments/orga/tournaments/{tournament.id}'
        '/qualification/draft"'
    ) in html
    assert 'data-lt-playoff-version' in html
    assert html.count('class="lt-seed-org"') == 4
    assert 'is-same' not in html


def test_embedded_draft_flags_and_separates_a_same_group_pairing(
    site_app, orga, make_tournament
):
    tournament = make_tournament.groups()
    _play_groups(tournament, orga)
    _draft_version(tournament)

    with http_client(site_app, user_id=orga.id) as client:
        clashing = _make_clash(client, tournament)
        flagged = client.get(_url(tournament, '/qualification')).get_data(
            as_text=True
        )
        response = client.post(
            _draft_url(tournament),
            data={
                'version': str(clashing['version']),
                'action': 'separate',
            },
            headers=JSON,
        )
        again = client.post(
            _draft_url(tournament),
            data={
                'version': str(response.get_json()['board']['version']),
                'action': 'separate',
            },
            headers=JSON,
        )

    assert 'lt-seed-match is-same' in flagged
    assert 'name="action" value="separate"' in flagged
    assert response.status_code == 200, response.get_json()
    board = response.get_json()['board']
    assert board['version'] == clashing['version'] + 1
    assert not any(m['same_group'] for m in board['layout']['matches'])
    assert again.status_code == 422
    db.session.rollback()
    events = [
        e.event_type
        for e in tournament_log_service.get_entries_for_tournament(
            tournament.id
        )
    ]
    assert events.count('seeding-separated') == 1


def _flip_top_pair(tournament, orga, margin=5):
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
    tournament_match_service.unconfirm_match(match.id, orga.id).unwrap()
    _confirm(match, ids, scores, orga)


def test_draft_route_reprefills_a_stale_draft(site_app, orga, make_tournament):
    tournament = make_tournament.groups()
    _play_groups(tournament, orga)
    _draft_version(tournament)

    with http_client(site_app, user_id=orga.id) as client:
        clashing = _make_clash(client, tournament)
        _flip_top_pair(tournament, orga)
        stale = _draft_board(tournament)
        assert stale.stale_ranks
        assert stale.version == clashing['version']
        page = client.get(_url(tournament, '/qualification')).get_data(
            as_text=True
        )
        response = client.post(
            _draft_url(tournament),
            data={'version': str(stale.version), 'action': 'reprefill'},
            headers=JSON,
        )

    assert 'name="action" value="reprefill"' in page
    assert response.status_code == 200, response.get_json()
    board = response.get_json()['board']
    assert board['stale_ranks'] is False
    assert board['stale'] is False
    assert board['version'] == stale.version + 1
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
    assert [e.initiator_id for e in entries] == [orga.id]


def test_draft_route_separate_with_a_stale_version_is_409(
    site_app, orga, make_tournament
):
    tournament = make_tournament.groups()
    _play_groups(tournament, orga)
    _draft_version(tournament)

    with http_client(site_app, user_id=orga.id) as client:
        clashing = _make_clash(client, tournament)
        response = client.post(
            _draft_url(tournament),
            data={
                'version': str(clashing['version'] - 1),
                'action': 'separate',
            },
            headers=JSON,
        )

    assert response.status_code == 409
    assert _draft_board(tournament).version == clashing['version']


def test_draft_route_without_json_goes_back_to_the_qualification_page(
    site_app, orga, make_tournament
):
    tournament = make_tournament.groups()
    _play_groups(tournament, orga)
    _draft_version(tournament)

    with http_client(site_app, user_id=orga.id) as client:
        clashing = _make_clash(client, tournament)
        response = client.post(
            _draft_url(tournament),
            data={'version': str(clashing['version']), 'action': 'separate'},
        )
        html = client.get(response.headers['Location']).get_data(as_text=True)

    assert response.status_code == 302
    assert response.headers['Location'].endswith('/qualification')
    assert 'Separated: ' in html
    assert 'is-same' not in html


def _qualification_path(tournament):
    return f'/lan-tournaments/orga/tournaments/{tournament.id}/qualification'


def _view_url(tournament, suffix=''):
    return f'{BASE_URL}/{tournament.id}{suffix}'


def _play_plain_rr_with_one_winner(tournament, initiator, *, leave_open=1):
    """Let one player win every match; the others draw, some stay open."""
    matches = [m for ms in _groups_of(tournament).values() for m in ms]
    winner = sorted({i for _, ids in matches for i in ids}, key=str)[0]
    others = [(m, ids) for m, ids in matches if winner not in ids]
    skipped = {m.id for m, _ in others[:leave_open]}
    for match, ids in matches:
        if match.id in skipped:
            continue
        scores = (
            (1, 1)
            if winner not in ids
            else ((1, 0) if ids[0] == winner else (0, 1))
        )
        _confirm(match, ids, scores, initiator)
    return skipped


def test_plain_round_robin_view_links_the_qualification_during_a_winner_tie(
    site_app, orga, make_tournament
):
    tournament = make_tournament.plain_rr()
    _play_all_draws(tournament, orga)
    assert _state(tournament).blockers

    with http_client(site_app, user_id=orga.id) as client:
        response = client.get(_view_url(tournament))

    assert response.status_code == 200
    assert _qualification_path(tournament) in response.get_data(as_text=True)


def test_plain_round_robin_view_has_no_qualification_link_while_it_runs(
    site_app, orga, make_tournament
):
    tournament = make_tournament.plain_rr()

    with http_client(site_app, user_id=orga.id) as client:
        response = client.get(_view_url(tournament))

    assert response.status_code == 200
    assert '/qualification' not in response.get_data(as_text=True)


def test_plain_round_robin_view_has_no_qualification_link_without_a_tie(
    site_app, orga, make_tournament
):
    tournament = make_tournament.plain_rr()
    _play_plain_rr_with_one_winner(tournament, orga)
    assert _state(tournament).open_match_count == 1

    with http_client(site_app, user_id=orga.id) as client:
        response = client.get(_view_url(tournament))
        bracket = client.get(_view_url(tournament, '/bracket'))

    assert response.status_code == 200
    assert '/qualification' not in response.get_data(as_text=True)
    assert 'data-lt-winner-tie' not in bracket.get_data(as_text=True)


def test_plain_round_robin_view_hides_the_qualification_link_from_others(
    site_app, bystander, orga, make_tournament
):
    tournament = make_tournament.plain_rr()
    _play_all_draws(tournament, orga)

    with http_client(site_app, user_id=bystander.id) as client:
        participant = client.get(_view_url(tournament))
    with http_client(site_app) as client:
        anonymous = client.get(_view_url(tournament))

    for response in (participant, anonymous):
        assert response.status_code == 200
        assert '/qualification' not in response.get_data(as_text=True)


def test_plain_round_robin_qualification_page_serves_the_winner_tie(
    site_app, orga, make_tournament
):
    tournament = make_tournament.plain_rr()
    _play_all_draws(tournament, orga)

    with http_client(site_app, user_id=orga.id) as client:
        response = client.get(_url(tournament, '/qualification'))

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'data-ranking="winner"' in html
    assert 'data-blocker="winner"' in html


def test_plain_round_robin_winner_decision_completes_the_tournament(
    site_app, orga, make_tournament
):
    tournament = make_tournament.plain_rr()
    _play_all_draws(tournament, orga)
    form = _decide_form(_state(tournament), 'winner')
    assert tournament_match_service.is_plain_round_robin(tournament)
    assert (
        lan_tournament_view_helpers.plain_round_robin_winner_tie(tournament)
        == 'open'
    )

    with http_client(site_app, user_id=orga.id) as client:
        response = client.post(
            _url(tournament, '/qualification/decisions'), data=form
        )

    assert response.status_code == 302
    db.session.rollback()
    current = tournament_repository.get_tournament(tournament.id)
    assert current.tournament_status is TournamentStatus.COMPLETED
    assert str(current.winner_participant_id) == form['order'][0]
    assert (
        lan_tournament_view_helpers.plain_round_robin_winner_tie(current)
        == 'decided'
    )


def test_site_bracket_names_the_open_winner_tie_to_orgas_only(
    site_app, orga, make_tournament
):
    tournament = make_tournament.plain_rr()
    _play_all_draws(tournament, orga)

    with http_client(site_app, user_id=orga.id) as client:
        as_orga = client.get(_view_url(tournament, '/bracket'))
    with http_client(site_app) as client:
        anonymous = client.get(_view_url(tournament, '/bracket'))

    assert as_orga.status_code == 200
    html = as_orga.get_data(as_text=True)
    assert 'data-lt-winner-tie' in html
    assert _qualification_path(tournament) in html
    assert anonymous.status_code == 200
    assert 'data-lt-winner-tie' not in anonymous.get_data(as_text=True)
