"""
tests.integration.services.lan_tournament.test_participant_playoff_views
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Drives the participant bracket page of a tournament with a playoff phase
through a real site app: the group tables with the cut line, the waiting
panel with exactly one reason, the playoff bracket with origin labels and,
above all, that nothing orga-only reaches a participant or a visitor.
"""

from datetime import datetime, UTC
from itertools import count
import json
import re

import pytest

from byceps.database import db
from byceps.services.lan_tournament import (
    seed_code,
    tournament_domain_service,
    tournament_match_service,
    tournament_qualification_service,
    tournament_repository,
    tournament_score_service,
    tournament_seeding_service,
    tournament_service,
    tournament_team_service,
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
from byceps.util.uuid import generate_uuid7

from tests.helpers import http_client, log_in_user


BASE_URL = 'http://www.acmecon.test/lan-tournaments'

REASON = 'Coin-flip-at-the-table-SECRETREASON'

_counter = count(1)

# Keys of orga-only data; none may appear in a participant payload.
FORBIDDEN_KEYS = frozenset(
    {
        'seed',
        'tier',
        'code',
        'fingerprint',
        'reason',
        'roster_snapshot',
        'seeding_target',
        'joined_late',
    }
)
_ALPHABET = re.escape(seed_code._ALPHABET)
SEED_CODE = re.compile(rf'\bS(?:-?[{_ALPHABET}]){{11,}}\b')
ORGA_ONLY_WORDS = re.compile(
    r'roster_snapshot|seeding_target|joined_late|fingerprint|'
    r'data-(?:seed|tier|code|reason|roster)',
    re.IGNORECASE,
)


@pytest.fixture(scope='module')
def players(make_user):
    users = [make_user(f'ParticipantPlayoffPlayer{i}') for i in range(8)]
    for user in users:
        log_in_user(user.id)
    return users


@pytest.fixture
def make_tournament(party, players):
    created = []

    def _create(name, **kwargs):
        result = tournament_service.create_tournament(
            party.id,
            f'{name} {next(_counter)}',
            **{'contestant_type': ContestantType.SOLO, **kwargs},
        )
        assert result.is_ok(), result.unwrap_err()
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
        return tournament

    def _start(tournament):
        started = tournament_service.change_status(
            tournament.id, TournamentStatus.ONGOING, players[0].id
        )
        assert started.is_ok(), started.unwrap_err()

    def _groups(release_mode=PlayoffReleaseMode.MANUAL):
        tournament = _create(
            'Participant Playoff Groups',
            game_format=GameFormat.ONE_V_ONE,
            elimination_mode=EliminationMode.ROUND_ROBIN,
            tournament_status=TournamentStatus.REGISTRATION_CLOSED,
            playoff_game_format=GameFormat.ONE_V_ONE,
            playoff_elimination_mode=EliminationMode.SINGLE_ELIMINATION,
            playoff_group_count=2,
            playoff_qualifiers_per_group=2,
            playoff_release_mode=release_mode,
        )
        generated = tournament_match_service.generate_round_robin_bracket(
            tournament.id
        )
        assert generated.is_ok(), generated.unwrap_err()
        _start(tournament)
        return tournament

    def _groups_draft():
        return _create(
            'Participant Playoff Groups Draft',
            game_format=GameFormat.ONE_V_ONE,
            elimination_mode=EliminationMode.ROUND_ROBIN,
            tournament_status=TournamentStatus.REGISTRATION_CLOSED,
            playoff_game_format=GameFormat.ONE_V_ONE,
            playoff_elimination_mode=EliminationMode.SINGLE_ELIMINATION,
            playoff_group_count=2,
            playoff_qualifiers_per_group=2,
            playoff_release_mode=PlayoffReleaseMode.MANUAL,
        )

    def _ffa():
        return _create(
            'Participant Free For All',
            game_format=GameFormat.FREE_FOR_ALL,
            elimination_mode=EliminationMode.SINGLE_ELIMINATION,
            tournament_status=TournamentStatus.REGISTRATION_CLOSED,
            group_size_min=2,
            group_size_max=4,
            advancement_count=2,
            point_table=[10, 6, 3, 1],
        )

    def _bracket(elimination_mode):
        tournament = _create(
            'Participant Playoff Bracket',
            game_format=GameFormat.ONE_V_ONE,
            elimination_mode=elimination_mode,
            tournament_status=TournamentStatus.REGISTRATION_CLOSED,
        )
        board = tournament_seeding_service.get_board(tournament.id).unwrap()
        generated = tournament_seeding_service.generate_from_seeding(
            tournament.id,
            expected_version=board.version,
            initiator_id=players[0].id,
        )
        assert generated.is_ok(), generated.unwrap_err()
        _start(tournament)
        return tournament

    def _highscore():
        return _create(
            'Participant Playoff Highscore',
            game_format=GameFormat.HIGHSCORE,
            elimination_mode=EliminationMode.NONE,
            tournament_status=TournamentStatus.ONGOING,
            playoff_game_format=GameFormat.FREE_FOR_ALL,
            playoff_elimination_mode=EliminationMode.SINGLE_ELIMINATION,
            playoff_qualifier_count=4,
            playoff_release_mode=PlayoffReleaseMode.MANUAL,
            point_table=[5, 3, 2, 1],
            group_size_min=3,
            group_size_max=4,
            advancement_count=2,
            score_ordering=ScoreOrdering.HIGHER_IS_BETTER,
        )

    def _plain_highscore():
        return _create(
            'Participant Plain Highscore',
            game_format=GameFormat.HIGHSCORE,
            elimination_mode=EliminationMode.NONE,
            tournament_status=TournamentStatus.ONGOING,
            score_ordering=ScoreOrdering.HIGHER_IS_BETTER,
        )

    class Factory:
        create = staticmethod(_create)
        plain_highscore = staticmethod(_plain_highscore)
        groups = staticmethod(_groups)
        groups_draft = staticmethod(_groups_draft)
        ffa = staticmethod(_ffa)
        bracket = staticmethod(_bracket)
        highscore = staticmethod(_highscore)

    yield Factory
    db.session.rollback()
    for tournament in created:
        if tournament_repository.find_tournament(tournament.id) is not None:
            tournament_service.delete_tournament(tournament.id)


# -------------------------------------------------------------------- #
# helpers


def _page(site_app, tournament, path='bracket', *, user=None, query=''):
    with http_client(
        site_app, user_id=user.id if user is not None else None
    ) as client:
        url = f'{BASE_URL}/{tournament.id}' + (f'/{path}' if path else '')
        response = client.get(url + query)
    assert response.status_code == 200, (path, query, response.status_code)
    return response.get_data(as_text=True)


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
            low, high = sorted(ids, key=str)
            scores = (margin, 0) if ids[0] == low else (0, margin)
            _confirm(match, ids, scores, initiator)


def _play_all_draws(tournament, initiator):
    """Draw every group match with the group number + 1 goals each."""
    for group, matches in _groups_of(tournament).items():
        for match, ids in matches:
            _confirm(match, ids, (group + 1, group + 1), initiator)


def _decide_ties(tournament, initiator):
    db.session.rollback()
    state = tournament_qualification_service.get_qualification(
        tournament.id
    ).unwrap()
    for block in state.blockers:
        decided = tournament_qualification_service.save_decision(
            tournament.id,
            block.scope,
            list(block.contestant_ids),
            reason=REASON,
            initiator_id=initiator.id,
        )
        assert decided.is_ok(), decided.unwrap_err()


def _release(tournament, initiator):
    board = tournament_seeding_service.ensure_playoff_draft(tournament.id)
    assert board.is_ok(), board.unwrap_err()
    released = tournament_qualification_service.release_playoffs(
        tournament.id,
        expected_version=board.unwrap().version,
        initiator_id=initiator.id,
    )
    assert released.is_ok(), released.unwrap_err()


def _fill_leaderboard(tournament, players, scores):
    db.session.rollback()
    participants = {
        p.user_id: p
        for p in tournament_repository.get_participants_for_tournament(
            tournament.id
        )
    }
    for user, score in zip(players, scores, strict=False):
        submitted = tournament_score_service.submit_score(
            tournament.id,
            score,
            participant_id=TournamentParticipantID(
                str(participants[user.id].id)
            ),
        )
        assert submitted.is_ok(), submitted.unwrap_err()


def _close_leaderboard(tournament, initiator):
    closed = tournament_score_service.close_leaderboard(
        tournament.id, initiator_id=initiator.id
    )
    assert closed.is_ok(), closed.unwrap_err()


def _rows_of(section_html):
    """Return the tbody row classes of a ranking section, in order."""
    return re.findall(r"<tr class='([^']*)'>", section_html)


def _sections(html):
    parts = html.split('data-lt-ranking')[1:]
    return [part.split('</section>')[0] for part in parts]


def _wait_reasons(html):
    return re.findall(r"data-lt-wait='([a-z]+)'", html)


def _bracket_json(html):
    found = re.search(
        r"<script type='application/json' id='bracket-data'>(.*?)</script>",
        html,
        re.DOTALL,
    )
    return json.loads(found.group(1)) if found else None


def _keys_of(value):
    if isinstance(value, dict):
        for key, inner in value.items():
            yield key
            yield from _keys_of(inner)
    elif isinstance(value, list):
        for inner in value:
            yield from _keys_of(inner)


def _assert_no_orga_data(html):
    """Fail if the page carries a seed, a tier, a code or a reason."""
    assert not SEED_CODE.search(html)
    assert not ORGA_ONLY_WORDS.search(html)
    assert 'SECRETREASON' not in html
    assert REASON not in html
    for comment in re.findall(r'<!--(.*?)-->', html, re.DOTALL):
        assert not re.search(
            r'seed|tier|fingerprint|reason', comment, re.IGNORECASE
        ), comment
    data = _bracket_json(html)
    if data is not None:
        assert not FORBIDDEN_KEYS & set(_keys_of(data))
        for contestant in (
            c for m in data['matches'] for c in m['contestants']
        ):
            assert set(contestant) == {
                'name',
                'score',
                'team_id',
                'participant_id',
                'origin',
            }


# -------------------------------------------------------------------- #
# group tables


def test_group_standings_show_cut_line(site_app, players, make_tournament):
    tournament = make_tournament.groups()
    _play_groups(tournament, players[0])

    html = _page(site_app, tournament, query='?phase=1')

    sections = _sections(html)
    assert len(sections) == 2
    for section in sections:
        rows = _rows_of(section)
        assert len(rows) == 5
        assert rows.count('lt-cut') == 1
        assert rows.index('lt-cut') == 2
        assert 'is-tie' not in rows
    assert html.count('lt-stw is-qualified') == 4
    assert html.count('lt-stw is-out') == 4
    assert 'lt-phase__link is-on' in html


def test_group_standings_mark_a_tie_and_an_orga_decision(
    site_app, players, make_tournament
):
    tournament = make_tournament.groups()
    _play_all_draws(tournament, players[0])

    tied = _page(site_app, tournament, query='?phase=1')

    assert 'lt-stw is-tie' in tied
    assert [
        _rows_of(section).count('is-tie') for section in _sections(tied)
    ] == [4, 4]
    assert all(
        _rows_of(section).count('lt-cut') == 1 for section in _sections(tied)
    )
    assert 'lt-tag--orga' not in tied

    _decide_ties(tournament, players[0])
    decided = _page(site_app, tournament, query='?phase=1')

    assert 'lt-tag--orga' in decided
    assert 'lt-stw is-tie' not in decided
    assert REASON not in decided


def test_group_standings_show_open_matches(site_app, players, make_tournament):
    tournament = make_tournament.groups()
    match, ids = next(iter(_groups_of(tournament).values()))[0]
    _confirm(match, ids, (1, 0), players[0])

    html = _page(site_app, tournament, query='?phase=1')

    assert html.count('lt-stw is-open') == 8
    assert 'matches open' in html or 'match open' in html


def test_leaderboard_shows_the_cut_line(site_app, players, make_tournament):
    tournament = make_tournament.highscore()
    _fill_leaderboard(tournament, players, [80, 70, 60, 50, 40, 30, 20, 10])
    _close_leaderboard(tournament, players[0])

    html = _page(site_app, tournament, query='?phase=1')

    (section,) = _sections(html)
    rows = _rows_of(section)
    assert rows.index('lt-cut') == 4
    assert len(rows) == 9
    assert html.count('lt-stw is-qualified') == 4


# -------------------------------------------------------------------- #
# waiting panel


def test_waiting_panel_reason(site_app, players, make_tournament):
    tournament = make_tournament.groups()

    running = _page(site_app, tournament, query='?phase=2')
    assert _wait_reasons(running) == ['groups']
    assert 'Playoffs: waiting for release' in running
    assert _wait_reasons(_page(site_app, tournament, query='?phase=1')) == []

    _play_all_draws(tournament, players[0])
    tied = _page(site_app, tournament, query='?phase=2')
    assert _wait_reasons(tied) == ['tie']
    wait_panel = tied.split("data-lt-wait='tie'")[1].split('</section>')[0]
    assert not any(user.screen_name in wait_panel for user in players)
    assert _wait_reasons(_page(site_app, tournament, query='?phase=1')) == []

    _decide_ties(tournament, players[0])
    ready_two = _page(site_app, tournament, query='?phase=2')
    ready_one = _page(site_app, tournament, query='?phase=1')
    assert _wait_reasons(ready_two) == ['release']
    assert _wait_reasons(ready_one) == ['release']

    _release(tournament, players[0])
    released = _page(site_app, tournament)
    assert _wait_reasons(released) == []
    assert _wait_reasons(_page(site_app, tournament, query='?phase=1')) == []


def test_waiting_panel_reason_of_a_leaderboard(
    site_app, players, make_tournament
):
    tournament = make_tournament.highscore()
    _fill_leaderboard(tournament, players, [80, 70, 60, 50, 40])

    open_ = _page(site_app, tournament, query='?phase=2')
    assert _wait_reasons(open_) == ['leaderboard']

    _close_leaderboard(tournament, players[0])
    closed = _page(site_app, tournament, query='?phase=2')
    assert _wait_reasons(closed) == ['release']
    assert _wait_reasons(_page(site_app, tournament, query='?phase=1')) == [
        'release'
    ]

    _release(tournament, players[0])
    released = _page(site_app, tournament)
    assert _wait_reasons(released) == []
    assert released.count('lt-lobby__head') == 1
    assert 'lt-phase__link is-on' in released


def test_the_phase_defaults_to_the_playoffs_once_released(
    site_app, players, make_tournament
):
    tournament = make_tournament.groups()
    _play_groups(tournament, players[0])

    before = _page(site_app, tournament)
    assert 'data-lt-ranking' in before
    assert "id='bracket-data'" not in before

    _release(tournament, players[0])
    after = _page(site_app, tournament)
    assert 'data-lt-ranking' not in after
    assert "id='bracket-data'" in after
    assert 'data-lt-ranking' in _page(site_app, tournament, query='?phase=1')
    assert 'data-lt-ranking' not in _page(
        site_app, tournament, query='?phase=junk'
    )


def test_a_tournament_without_playoffs_has_no_phase_switch(
    site_app, make_tournament
):
    tournament = make_tournament.bracket(EliminationMode.SINGLE_ELIMINATION)

    html = _page(site_app, tournament)

    assert 'lt-phase' not in html
    assert "id='bracket-data'" in html


def _phase_link(html):
    return re.search(r"<a class='lt-phase__link'\s+href='([^']*)'", html)


@pytest.mark.parametrize('as_user', [True, False])
def test_highscore_page_links_to_the_playoff_phase(
    site_app, players, make_tournament, as_user
):
    tournament = make_tournament.highscore()
    user = players[1] if as_user else None

    html = _page(site_app, tournament, 'highscore', user=user)

    link = _phase_link(html)
    assert link is not None
    assert link.group(1).endswith(f'/{tournament.id}/bracket?phase=2')
    assert 'lan_tournament_standings.css' in html


@pytest.mark.parametrize('as_user', [True, False])
def test_highscore_page_without_playoffs_has_no_phase_link(
    site_app, players, make_tournament, as_user
):
    tournament = make_tournament.plain_highscore()
    user = players[1] if as_user else None

    html = _page(site_app, tournament, 'highscore', user=user)

    assert 'lt-phase' not in html
    assert 'lan_tournament_standings.css' not in html


def _head_of(html):
    found = re.search(r"<header class='head'>(.*?)</header>", html, re.DOTALL)
    return found.group(1) if found else None


def test_highscore_page_shows_the_cut_and_status(
    site_app, players, make_tournament
):
    tournament = make_tournament.highscore()
    _fill_leaderboard(tournament, players, [80, 70, 60, 50, 40, 30, 20, 10])
    _close_leaderboard(tournament, players[0])

    html = _page(site_app, tournament, 'highscore', user=players[1])

    (section,) = _sections(html)
    rows = _rows_of(section)
    assert rows.index('lt-cut') == 4
    assert len(rows) == 9
    assert html.count('lt-stw is-qualified') == 4
    assert html.count('lt-stw is-out') == 4
    assert 'lt-lobbies' not in html


def test_highscore_page_hides_the_submit_form_after_close(
    site_app, players, make_tournament
):
    tournament = make_tournament.highscore()
    _fill_leaderboard(tournament, players, [80, 70, 60, 50, 40, 30, 20, 10])

    before = _page(site_app, tournament, 'highscore', user=players[1])
    _close_leaderboard(tournament, players[0])
    after = _page(site_app, tournament, 'highscore', user=players[1])

    assert "class='score-submit'" in before
    assert "class='score-submit'" not in after
    assert 'highscore/submit' not in after


def test_bracket_page_has_a_head(site_app, players, make_tournament):
    tournament = make_tournament.groups()

    html = _page(site_app, tournament, 'bracket', user=players[1])

    head = _head_of(html)
    assert head is not None
    assert f"<h1 class='title'>{tournament.name}</h1>" in head
    assert 'Ongoing' in head


@pytest.mark.parametrize('contestant', ['solo', 'team'])
@pytest.mark.parametrize('with_playoffs', [False, True])
@pytest.mark.parametrize(
    'status', [TournamentStatus.REGISTRATION_OPEN, TournamentStatus.ONGOING]
)
def test_submit_form_shows_for_participants_in_registration_and_ongoing(
    site_app, players, make_tournament, status, with_playoffs, contestant
):
    kwargs = {
        'game_format': GameFormat.HIGHSCORE,
        'elimination_mode': EliminationMode.NONE,
        'tournament_status': status,
        'score_ordering': ScoreOrdering.HIGHER_IS_BETTER,
    }
    if with_playoffs:
        kwargs |= {
            'playoff_game_format': GameFormat.FREE_FOR_ALL,
            'playoff_elimination_mode': EliminationMode.SINGLE_ELIMINATION,
            'playoff_qualifier_count': 4,
            'playoff_release_mode': PlayoffReleaseMode.MANUAL,
            'point_table': [5, 3, 2, 1],
            'group_size_min': 3,
            'group_size_max': 4,
            'advancement_count': 2,
        }
    if contestant == 'team':
        kwargs |= {
            'contestant_type': ContestantType.TEAM,
            'min_players_in_team': 1,
            'max_players_in_team': 2,
        }
    tournament = make_tournament.create('Participant Submit Form', **kwargs)

    if contestant == 'team':
        # A participant without a team cannot submit, the captain can.
        loner = _page(site_app, tournament, 'highscore', user=players[2])
        assert "class='score-submit'" not in loner
        created = tournament_team_service.create_team(
            tournament.id, 'Submit Team', players[1].id
        )
        assert created.is_ok(), created.unwrap_err()
        db.session.commit()

    html = _page(site_app, tournament, 'highscore', user=players[1])

    assert "class='score-submit'" in html
    assert f'/{tournament.id}/highscore/submit' in html


def test_submit_form_hidden_for_non_participants_and_after_release(
    site_app, players, make_user, make_tournament
):
    outsider = make_user('ParticipantPlayoffOutsider')
    log_in_user(outsider.id)
    tournament = make_tournament.highscore()

    def form_for(user):
        html = _page(site_app, tournament, 'highscore', user=user)
        return "class='score-submit'" in html

    assert form_for(players[1])
    assert not form_for(outsider)
    assert not form_for(None)

    _fill_leaderboard(tournament, players, [80, 70, 60, 50, 40, 30, 20, 10])
    _close_leaderboard(tournament, players[0])
    tournament_seeding_service.ensure_playoff_draft(tournament.id).unwrap()
    _release(tournament, players[0])

    assert not form_for(players[1])
    released = _page(site_app, tournament, 'highscore', user=players[1])
    assert 'lt-lobbies' in released
    assert len(_sections(released)) == 1


# -------------------------------------------------------------------- #
# playoff bracket


def test_playoff_bracket_carries_phase_and_origin_labels(
    site_app, players, make_tournament
):
    tournament = make_tournament.groups()
    _play_groups(tournament, players[0])
    _release(tournament, players[0])

    html = _page(site_app, tournament)

    data = _bracket_json(html)
    assert {m['phase'] for m in data['matches']} == {2}
    assert data['tournament']['elimination_mode'] == 'SINGLE_ELIMINATION'
    origins = {
        c['origin']
        for m in data['matches']
        for c in m['contestants']
        if c['origin']
    }
    assert origins == {'A1', 'A2', 'B1', 'B2'}
    assert 'Playoffs · Semifinal 1' in html
    assert 'Group A' in html
    assert re.search(r"href='[^']*/matches/[^']*'", html)


# -------------------------------------------------------------------- #
# visibility


def _scenario_se(make_tournament, players):
    return make_tournament.bracket(EliminationMode.SINGLE_ELIMINATION)


def _scenario_de(make_tournament, players):
    return make_tournament.bracket(EliminationMode.DOUBLE_ELIMINATION)


def _scenario_groups_before(make_tournament, players):
    tournament = make_tournament.groups()
    _play_all_draws(tournament, players[0])
    _decide_ties(tournament, players[0])
    tournament_seeding_service.ensure_playoff_draft(tournament.id).unwrap()
    return tournament


def _scenario_groups_after(make_tournament, players):
    tournament = _scenario_groups_before(make_tournament, players)
    _release(tournament, players[0])
    return tournament


def _scenario_highscore_before(make_tournament, players):
    tournament = make_tournament.highscore()
    _fill_leaderboard(tournament, players, [80, 70, 60, 50, 40, 30, 20, 10])
    _close_leaderboard(tournament, players[0])
    tournament_seeding_service.ensure_playoff_draft(tournament.id).unwrap()
    return tournament


def _scenario_highscore_after(make_tournament, players):
    tournament = _scenario_highscore_before(make_tournament, players)
    _release(tournament, players[0])
    return tournament


SCENARIOS = {
    'se': _scenario_se,
    'de': _scenario_de,
    'groups-before-release': _scenario_groups_before,
    'groups-after-release': _scenario_groups_after,
    'highscore-before-release': _scenario_highscore_before,
    'highscore-after-release': _scenario_highscore_after,
}


@pytest.mark.parametrize('visitor', ['participant', 'anonymous'])
@pytest.mark.parametrize('scenario', list(SCENARIOS))
def test_participant_html_and_json_have_no_seeds_or_tiers(
    site_app, players, make_tournament, scenario, visitor
):
    tournament = SCENARIOS[scenario](make_tournament, players)
    user = players[1] if visitor == 'participant' else None

    pages = [
        _page(site_app, tournament, 'bracket', user=user),
        _page(site_app, tournament, 'bracket', user=user, query='?phase=1'),
        _page(site_app, tournament, 'bracket', user=user, query='?phase=2'),
        _page(site_app, tournament, 'matches', user=user),
        _page(site_app, tournament, '', user=user),
    ]
    if tournament.game_format == GameFormat.HIGHSCORE:
        pages.append(_page(site_app, tournament, 'highscore', user=user))

    for html in pages:
        _assert_no_orga_data(html)


def _seed_and_generate(tournament, initiator, *actions):
    db.session.rollback()
    board = tournament_seeding_service.get_board(tournament.id).unwrap()
    for action in actions:
        board = tournament_seeding_service.apply_action(
            tournament.id,
            'initial',
            action,
            expected_version=board.version,
            initiator_id=initiator.id,
        ).unwrap()
    generated = tournament_seeding_service.generate_from_seeding(
        tournament.id,
        expected_version=board.version,
        initiator_id=initiator.id,
    )
    assert generated.is_ok(), generated.unwrap_err()
    db.session.rollback()
    return board


def _name_by_contestant(tournament, players):
    names = {user.id: user.screen_name for user in players}
    return {
        str(p.id): names[p.user_id]
        for p in tournament_repository.get_participants_for_tournament(
            tournament.id
        )
    }


def _match_page(site_app, match):
    with http_client(site_app) as client:
        response = client.get(f'{BASE_URL}/matches/{match.id}')
    assert response.status_code == 200, response.status_code
    return response.get_data(as_text=True)


@pytest.mark.parametrize('layout_kind', ['ffa_tiers', 'rr_groups'])
def test_public_lobby_and_group_order_follows_contestant_ids(
    site_app, players, make_tournament, layout_kind
):
    if layout_kind == 'ffa_tiers':
        tournament = make_tournament.ffa()
        board = _seed_and_generate(
            tournament, players[0], tournament_seeding_service.SetTierCount(2)
        )
        layout = [cid for cid in board.state.layout if cid is not None]
        slices = [set(layout[0:4]), set(layout[4:8])]
        names = _name_by_contestant(tournament, players)
        matches = [
            m
            for m in tournament_repository.get_matches_for_tournament(
                tournament.id
            )
            if m.round == 0
        ]
        assert len(matches) == 2
        members = []
        for match in matches:
            ids = [
                str(c.participant_id)
                for c in tournament_repository.get_contestants_for_match(
                    match.id
                )
            ]
            assert ids == sorted(ids)
            members.append(set(ids))
            html = _match_page(site_app, match)
            positions = [html.find(names[cid]) for cid in ids]
            assert -1 not in positions
            assert positions == sorted(positions)
        assert sorted(members, key=sorted) == sorted(slices, key=sorted)
    else:
        tournament = make_tournament.groups_draft()
        board = _seed_and_generate(
            tournament, players[0], tournament_seeding_service.Swap(0, 7)
        )
        layout = [cid for cid in board.state.layout if cid is not None]
        slices = [layout[0:4], layout[4:8]]
        groups = _groups_of(tournament)
        assert len(groups) == 2
        for entries in groups.values():
            round_zero = {
                frozenset(str(i) for i in ids)
                for match, ids in entries
                if match.round == 0
            }
            expected = {
                frozenset(pair)
                for pair in tournament_domain_service.generate_round_robin_schedule(
                    sorted(
                        next(
                            s
                            for s in slices
                            if {str(i) for _, ids in entries for i in ids}
                            == set(s)
                        )
                    )
                )[0]
            }
            assert round_zero == expected


def test_the_orga_only_pattern_really_matches_a_seed_code():
    code = seed_code.format_seed_code(
        'S' + seed_code._ALPHABET[:14] + seed_code._ALPHABET[3:5]
    )

    assert SEED_CODE.search(f'<p>{code}</p>')
    assert SEED_CODE.search('<p>' + code.replace('-', '') + '</p>')
    assert not SEED_CODE.search('<p>Standings STATUS Single</p>')
