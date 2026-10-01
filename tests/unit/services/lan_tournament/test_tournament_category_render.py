from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from byceps.services.lan_tournament import tournament_domain_service
from byceps.services.lan_tournament.blueprints.site import views
from byceps.services.lan_tournament.lan_tournament_view_helpers import (
    group_tournaments_by_category,
)
from byceps.services.lan_tournament.models.tournament_category import (
    TournamentCategory,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)

from .test_tournament_request_index_nav import (
    _BASE_INDEX_TEMPLATE,
    _BOTE_INDEX_TEMPLATE,
    _make_env,
    _render_index,
    _snippet,
)


def _tournament(name, category, position, **kwargs):
    return tournament_domain_service.create_tournament(
        'party',
        name,
        category=category,
        position=position,
        **kwargs,
    )[0]


def _public_context(tournaments):
    with (
        patch.object(views, '_get_current_party_or_404'),
        patch.object(views, 'tournament_service') as service,
        patch.object(views, 'tournament_team_service') as teams,
    ):
        service.get_tournaments_for_party.return_value = tournaments
        service.get_participant_counts_for_tournaments.return_value = {
            t.id: 3 for t in tournaments
        }
        teams.get_team_counts_for_tournaments.return_value = {}
        context = views.index.__wrapped__()
        service.get_participant_counts_for_tournaments.assert_called_once_with(
            [t.id for t in context['tournaments']]
        )
        return context


@pytest.mark.parametrize('path', [_BASE_INDEX_TEMPLATE, _BOTE_INDEX_TEMPLATE])
def test_public_groups_keep_cards_positions_and_hide_draft_only_categories(
    path,
):
    first = _tournament(
        'Z first',
        TournamentCategory.MAIN,
        2,
        tournament_status=TournamentStatus.REGISTRATION_OPEN,
        max_players=8,
        image_url='https://example.org/cover.png',
        image_alt_text='Cover text',
    )
    last = _tournament(
        'A last',
        TournamentCategory.MAIN,
        9,
        tournament_status=TournamentStatus.ONGOING,
    )
    fun = _tournament(
        'Fun card',
        TournamentCategory.FUN,
        0,
        tournament_status=TournamentStatus.PAUSED,
    )
    user = _tournament(
        'User card',
        TournamentCategory.USER_ORGANIZED,
        0,
        tournament_status=TournamentStatus.COMPLETED,
    )
    draft = _tournament(
        'Secret draft',
        TournamentCategory.STAGE,
        0,
        tournament_status=TournamentStatus.DRAFT,
    )
    no_status = _tournament('Invalid status', TournamentCategory.STAGE, 1)
    context = _public_context([last, fun, draft, no_status, user, first])
    env = _make_env({'index': _snippet(path)})
    html = _render_index(env, authenticated=True, **context)
    assert 'Secret draft' not in html and 'Invalid status' not in html
    assert 'data-tournament-category="STAGE"' not in html
    assert (
        html.index('Z first')
        < html.index('A last')
        < html.index('Fun card')
        < html.index('User card')
    )
    assert 'src="https://example.org/cover.png" alt="Cover text"' in html
    assert '3 / 8' in html
    assert 'Registration open' in html and 'Ongoing' in html
    assert 'Paused' in html and 'Completed' in html
    assert 'href="/view"' in html
    assert 'href="/propose_form"' in html and 'href="/my_requests"' in html
    for category in TournamentCategory:
        assert f'data-category-filter="{category.value}"' in html
    assert 'data-category-filter="ALL" aria-pressed="true"' in html


@pytest.mark.parametrize('path', [_BASE_INDEX_TEMPLATE, _BOTE_INDEX_TEMPLATE])
def test_public_all_hidden_has_no_category_heading(path):
    draft = _tournament(
        'Secret',
        TournamentCategory.MAIN,
        0,
        tournament_status=TournamentStatus.DRAFT,
    )
    context = _public_context([draft])
    env = _make_env({'index': _snippet(path)})
    html = _render_index(env, authenticated=False, **context)
    assert 'data-tournament-category=' not in html
    assert 'Secret' not in html
    assert 'No tournaments found.' in html or 'tourney-empty' in html


@pytest.mark.parametrize('can_update', [False, True])
@pytest.mark.parametrize('template', ['index', 'overview'])
def test_admin_filter_available_for_readers_and_editors(can_update, template):
    path = Path(
        'byceps/services/lan_tournament/blueprints/admin/templates'
        f'/admin/lan_tournament/{template}.html'
    )
    env = _make_env({'index': _snippet(path)})
    env.globals['render_extra_in_heading'] = str
    main = _tournament('Main cup', TournamentCategory.MAIN, 0)
    draft = _tournament(
        'Stage draft',
        TournamentCategory.STAGE,
        0,
        tournament_status=TournamentStatus.DRAFT,
    )
    html = env.get_template('index').render(
        tournaments=[main, draft],
        tournament_groups=group_tournaments_by_category([main, draft]),
        party=SimpleNamespace(id='party'),
        team_counts={},
        participant_counts={},
        elimination_mode_labels={},
        email_templates_configured=True,
        stats=SimpleNamespace(
            tournament_count=2,
            total_participant_count=0,
            ongoing_count=0,
            registration_open_count=0,
        ),
        g=SimpleNamespace(
            user=SimpleNamespace(
                has_permission=lambda permission: can_update,
            )
        ),
    )
    for category in TournamentCategory:
        assert f'data-category-filter="{category.value}"' in html
    assert 'data-category-filter="ALL" aria-pressed="true"' in html
    assert 'Main cup' in html and 'Stage draft' in html
    assert 'data-tournament-category="FUN" data-tournament-count="0"' in html
    assert ('class="drag-handle"' in html) == (
        can_update and template == 'index'
    )
