from pathlib import Path
from dataclasses import replace
from datetime import datetime, UTC
from types import SimpleNamespace

import pytest

from byceps.services.lan_tournament.models.contestant_status import (
    ContestantStatus,
)
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.tournament_personal_service import (
    PersonalGroup,
    PersonalMatch,
    PersonalTournament,
    evaluate_participation,
)

from .test_tournament_personal_service import (
    match,
    participant,
    side,
    tournament,
)
from .test_tournament_request_index_nav import _make_env


@pytest.fixture
def render():
    directory = Path(
        'byceps/services/lan_tournament/blueprints/site/templates/site/lan_tournament'
    )
    env = _make_env(
        {
            'overview': (directory / '_personal_overview.html').read_text(),
            'site/lan_tournament/_personal_contacts.html': (
                directory / '_personal_contacts.html'
            ).read_text(),
            'macros/misc.html': Path(
                'byceps/services/core/blueprints/common/templates/macros/misc.html'
            ).read_text(),
            'macros/icons.html': '{% macro render_icon(name) %}{% endmacro %}',
            'macros/seating.html': Path(
                'byceps/services/seating/blueprints/site/templates/macros/seating.html'
            ).read_text(),
        }
    )
    env.globals['ngettext'] = lambda singular, plural, count: (
        singular if count == 1 else plural
    )
    env.filters['fallback'] = lambda value, default: value or default
    original_url_for = env.globals['url_for']
    env.globals['url_for'] = lambda endpoint, **kwargs: (
        f'/matches/{kwargs["match_id"]}'
        if endpoint == '.view_match'
        else original_url_for(endpoint, **kwargs)
    )

    def render_entry(entry, **kwargs):
        return env.get_template('overview').render(
            overview_mode='personal',
            personal_groups={entry.group: [entry]},
            g=SimpleNamespace(user=SimpleNamespace(authenticated=True)),
            has_orga_assignments=False,
            categories=[],
            total_count=1,
            teams_by_id={},
            participants_by_id={},
            contact_users_by_id={},
            seats_by_user_id={},
            match_label=lambda m: 'Round 1 · Match 1',
            **{'orgas_by_tournament': {}, **kwargs},
        )

    return render_entry


@pytest.mark.parametrize(
    'confirmed,opponents,label',
    [
        (False, True, 'Match open'),
        (False, False, 'Opponent pending'),
        (True, True, 'Confirmed'),
        (True, False, 'DEFWIN'),
    ],
)
def test_match_state_is_saved_state_and_personal_review_remains_visible(
    render, confirmed, opponents, label
):
    t = tournament()
    p = participant(t)
    m = match(t, confirmed_by=p.user_id if confirmed else None)
    own = side(
        m, p, score=0, placement=2, contestant_status=ContestantStatus.DQ
    )
    entry = PersonalTournament(
        tournament=t,
        participant=p,
        group=PersonalGroup.WAITING,
        status='Participation needs review',
        needs_attention=True,
        matches=[
            PersonalMatch(
                match=m, own_side=own, opponents=[side(m)] if opponents else []
            )
        ],
    )
    html = render(entry)
    assert label in html
    assert 'personal-match-state--open' not in html
    assert (
        f'personal-match-state--{"confirmed" if confirmed else "notice"}'
        in html
    )
    assert 'Participation needs review' in html
    assert (
        'Score: 0' in html
        and 'Placement: 2' in html
        and 'Disqualified (match)' in html
    )
    actions = html.split('class="personal-match-actions"')[1].split('</div>')[0]
    assert 'View details' in actions and 'color-primary' not in actions
    assert html.count('personal-format-action') == 1
    assert 'Unknown' in html if opponents else 'Unknown' not in html


@pytest.mark.parametrize(
    'format,mode,label',
    [
        (
            GameFormat.FREE_FOR_ALL,
            EliminationMode.SINGLE_ELIMINATION,
            'View standings',
        ),
        (GameFormat.ONE_V_ONE, EliminationMode.ROUND_ROBIN, 'View standings'),
        (
            GameFormat.HIGHSCORE,
            EliminationMode.NONE,
            'Leaderboard',
        ),
    ],
)
def test_format_action_once_and_no_false_bracket_label(
    render, format, mode, label
):
    t = tournament(game_format=format, elimination_mode=mode)
    p = participant(t)
    matches = []
    if format != GameFormat.HIGHSCORE:
        for _ in range(4):
            m = match(t)
            matches.append(
                PersonalMatch(
                    match=m, own_side=side(m, p), opponents=[side(m), side(m)]
                )
            )
    entry = PersonalTournament(
        tournament=t,
        participant=p,
        group=PersonalGroup.WAITING,
        status='Paused',
        needs_attention=True,
        matches=matches,
    )
    html = render(entry)
    assert html.count('personal-format-action') == 1
    assert label in html and 'View Bracket' not in html
    assert 'Go to match' not in html
    assert html.count('View details') == len(matches)
    assert 'Paused' in html
    if matches:
        assert '<details class="personal-match-details">' not in html
        assert html.count('class="personal-opponent"') == 8


@pytest.mark.parametrize(
    'group', [PersonalGroup.REGISTERED, PersonalGroup.FINISHED]
)
def test_compact_entries_keep_format_and_finished_result(render, group):
    t = tournament()
    p = participant(t)
    m = match(t, confirmed_by=p.user_id)
    entry = PersonalTournament(
        tournament=t,
        participant=p,
        group=group,
        status=group.value,
        matches=[
            PersonalMatch(match=m, own_side=side(m, p, score=3), opponents=[])
        ],
    )
    html = render(entry)
    assert 'personal-match-box' not in html
    assert 'View Bracket' in html
    if group == PersonalGroup.FINISHED:
        assert html.index('Score: 3') < html.index('<details')
        assert 'Match history' in html
    else:
        assert 'Go to match' not in html


def test_all_ready_matches_visible_with_mixed_waiting_area_and_single_card(
    render,
):
    t = tournament(elimination_mode=EliminationMode.ROUND_ROBIN)
    p = participant(t)
    pending = match(t, round=0)
    ready = [match(t, round=i) for i in range(1, 6)]
    entry = evaluate_participation(
        t,
        p,
        [pending, *ready],
        {
            pending.id: [side(pending, p)],
            **{m.id: [side(m, p), side(m)] for m in ready},
        },
    )
    html = render(entry)
    assert html.count('data-personal-tournament=') == 1
    assert 'Up now <small>(1)</small>' in html
    assert html.count('Go to match') == 5
    assert html.count('View details') == 1
    assert '<details' not in html
    assert html.count('personal-format-action') == 1
    active, waiting = html.split('<section class="personal-waiting-matches"', 1)
    assert all(str(m.id) in active for m in ready)
    assert str(pending.id) in waiting
    assert 'personal-match-state--waiting' in waiting
    assert 'aria-hidden="true">⌛' in waiting
    assert 'aria-hidden="true">●' in active


def test_waiting_only_has_no_active_section_or_primary_match_link(render):
    t = tournament()
    p = participant(t)
    m = match(t)
    entry = evaluate_participation(t, p, [m], {m.id: [side(m, p)]})
    html = render(entry)
    assert 'data-personal-group="waiting"' in html
    assert 'data-personal-group="ongoing"' not in html
    assert 'Opponent pending' in html
    assert 'View details' in html and 'Go to match' not in html


def test_confirmed_history_uses_green_check_and_quiet_details(render):
    t = tournament()
    p = participant(t)
    m = match(t, confirmed_by=p.user_id)
    entry = evaluate_participation(
        t, p, [m], {m.id: [side(m, p, score=3), side(m, score=0)]}
    )
    html = render(entry)
    assert '<details class="personal-match-details">' in html
    assert 'personal-match-state--confirmed' in html
    assert 'aria-hidden="true">✓' in html
    assert 'View details' in html and 'Go to match' not in html


@pytest.mark.parametrize('complete', [False, True])
def test_highscore_waiting_badge_and_explicit_leaderboard_action(
    render, complete
):
    t = tournament(
        game_format=GameFormat.HIGHSCORE, elimination_mode=EliminationMode.NONE
    )
    entry = evaluate_participation(
        t, participant(t), [], {}, highscore_results_complete=complete
    )
    html = render(entry)
    assert ('data-personal-group="waiting"' in html) == complete
    assert (
        'Waiting for the tournament orga to complete the tournament' in html
    ) == complete
    assert ('Submit a score / Leaderboard' in html) == (not complete)
    assert 'personal-format-action' in html and 'Leaderboard' in html
    assert 'tag personal-status personal-status--' in html
    assert html.count('personal-format-action') == 1


@pytest.mark.parametrize('with_start', [False, True])
def test_missing_public_orga_omits_contacts_and_empty_footer(
    render, with_start
):
    t = tournament()
    if with_start:
        t = replace(t, start_time=datetime(2026, 10, 3, tzinfo=UTC))
    html = render(evaluate_participation(t, participant(t), [], {}))
    assert 'class="personal-orgas"' not in html
    assert 'No tournament orga assigned' not in html
    assert ('class="personal-footer"' in html) == with_start
    assert ('class="tournament-start"' in html) == with_start
