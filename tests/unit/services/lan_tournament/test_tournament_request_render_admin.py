"""
tests.unit.services.lan_tournament.test_tournament_request_render_admin
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from datetime import datetime
import html
import json
import pathlib
import re
from types import SimpleNamespace

from flask import g as flask_g
from jinja2 import DictLoader, Environment, StrictUndefined
from markupsafe import Markup
import pytest

from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.tournament import TournamentID
from byceps.services.lan_tournament.models.tournament_request import (
    TournamentRequest,
    TournamentRequestID,
    TournamentRequestStatus,
)
from byceps.services.lan_tournament.tournament_request_domain_service import (
    MAX_PARTICIPANT_LIMIT,
)
from byceps.services.party.models import PartyID
from byceps.services.user.models import UserID
from byceps.util.navigation import Navigation

from tests.helpers import generate_uuid


_VIEW_REQUEST_TEMPLATE = pathlib.Path(
    'byceps/services/lan_tournament/blueprints/admin/templates'
    '/admin/lan_tournament/view_request.html'
)
_QUEUE_TEMPLATE = pathlib.Path(
    'byceps/services/lan_tournament/blueprints/admin/templates'
    '/admin/lan_tournament/requests_for_party.html'
)
_LAYOUT_TEMPLATE = pathlib.Path(
    'byceps/services/lan_tournament/blueprints/admin/templates'
    '/layout/admin/lan_tournament.html'
)
_CREATE_FORM_TEMPLATE = pathlib.Path(
    'byceps/services/lan_tournament/blueprints/admin/templates'
    '/admin/lan_tournament/create_form.html'
)
_MATCH_ADMIN_CSS = pathlib.Path(
    'byceps/static/style/lan_tournament_match_admin.css'
)
_UPDATE_REQUEST_FORM_TEMPLATE = pathlib.Path(
    'byceps/services/lan_tournament/blueprints/admin/templates'
    '/admin/lan_tournament/update_request_form.html'
)

_MACROS_MISC = """
{% macro render_tag(label, class=None, icon=None, title=None) -%}
<span class="tag{% if class %} {{ class }}{% endif %}">{{ label }}</span>
{%- endmacro %}

{% macro render_notification_block(category=None, icon=None) -%}
<div class="notification{% if category %} color-{{ category }}{% endif %}">{{ caller() }}</div>
{%- endmacro %}
"""

_MACROS_FORMS = """
{% macro form_field_errors(field) -%}
{%- if field.errors %}<ol class="form-errors">
{%- for error in field.errors %}<li><strong>Error:</strong> <span>{{ error }}</span></li>{% endfor -%}
</ol>{% endif -%}
{%- endmacro %}
"""

_MACROS_ADMIN = """
{% macro render_extra_in_heading(value, label=None) -%}
<small>{{ value }}{% if label %} {{ label }}{% endif %}</small>
{%- endmacro %}
"""


def _make_env(templates):
    e = Environment(
        undefined=StrictUndefined,
        autoescape=True,
        loader=DictLoader(templates),
    )
    e.globals['_'] = lambda s, **kw: (s % kw) if kw else s
    e.globals['ngettext'] = lambda s, p, n, **kw: (
        (s if n == 1 else p) % {**kw, 'num': n}
    )
    e.globals['url_for'] = lambda endpoint, **kw: endpoint
    return e


def _make_gap(*, blocking=(), info_only=('preferred_end_time',)):
    return SimpleNamespace(
        # Issue-1 split: 12 supplied (`contestant_type` derived from
        # `preferred_end_time` moved to `info_only` -- it has no
        # `create_tournament` counterpart at all.
        supplied=[
            'name',
            'game',
            'game_format',
            'elimination_mode',
            'contestant_type',
            'team_size',
            'participant_limit',
            'preferred_start_time',
            'description',
            'ruleset',
            'party',
            'origin',
        ],
        admin_fills=[
            'score_ordering',
            'min_players',
            'min_teams',
            'advancement_count',
            'points_carry_to_losers',
            'image_url',
        ],
        blocking=list(blocking),
        info_only=list(info_only),
    )


def _make_request(
    *,
    request_id='r1',
    number=142,
    name='Some Tournament',
    proposer_id='u1',
    status='submitted',
    game_format_label='Free-for-All',
    elimination_mode_name='SINGLE_ELIMINATION',
    tournament_deleted=False,
    is_terminal=False,
    game='Some Game',
    team_size=1,
    participant_limit=16,
    preferred_start_time=datetime(2026, 1, 1, 10, 0),
    preferred_end_time=datetime(2026, 1, 1, 18, 0),
    created_at=datetime(2025, 12, 20, 9, 0),
):
    return SimpleNamespace(
        id=request_id,
        number=number,
        name=name,
        proposer_id=proposer_id,
        status=SimpleNamespace(value=status),
        game_format=SimpleNamespace(label=game_format_label),
        elimination_mode=EliminationMode[elimination_mode_name],
        tournament_deleted=tournament_deleted,
        # A `SimpleNamespace` has no computed properties, unlike the
        # real `TournamentRequest` dataclass -- set explicitly.
        is_terminal=is_terminal,
        game=game,
        team_size=team_size,
        participant_limit=participant_limit,
        preferred_start_time=preferred_start_time,
        preferred_end_time=preferred_end_time,
        created_at=created_at,
    )


_ELIMINATION_MODE_LABELS_FOR_TESTS = {
    EliminationMode.SINGLE_ELIMINATION: 'Single Elimination',
    EliminationMode.DOUBLE_ELIMINATION: 'Double Elimination',
    EliminationMode.ROUND_ROBIN: 'Round Robin',
    EliminationMode.NONE: 'None',
}


# --------------------------------------------------------------------- #
# view_request.html -- gap preview
# --------------------------------------------------------------------- #


@pytest.fixture(scope='module')
def gap_preview_env():
    src = _VIEW_REQUEST_TEMPLATE.read_text()
    start = src.index(
        '{# Gap preview: what create_tournament still needs beyond '
        'this request #}'
    )
    end = src.index('{# History #}', start)
    fragment = src[start:end]

    template = "{% from 'macros/misc.html' import render_tag %}\n" + fragment

    env = _make_env({'gap_preview': template, 'macros/misc.html': _MACROS_MISC})
    env.filters['dateformat'] = lambda dt, *a, **k: dt.strftime('%Y-%m-%d')
    env.filters['timeformat'] = lambda dt, *a, **k: dt.strftime('%H:%M')
    return env


def _render_gap_preview(
    env,
    *,
    blocking,
    is_terminal=False,
    tournament_deleted=False,
    info_only=('preferred_end_time',),
    tournament_request=None,
    party=None,
    elimination_mode_labels=None,
):
    tmpl = env.get_template('gap_preview')
    if tournament_request is None:
        tournament_request = _make_request(
            is_terminal=is_terminal, tournament_deleted=tournament_deleted
        )
    if party is None:
        party = SimpleNamespace(title='Some Party')
    if elimination_mode_labels is None:
        elimination_mode_labels = _ELIMINATION_MODE_LABELS_FOR_TESTS
    return tmpl.render(
        gap=_make_gap(blocking=blocking, info_only=info_only),
        tournament_request=tournament_request,
        party=party,
        elimination_mode_labels=elimination_mode_labels,
    )


def test_view_request_renders_gap_preview_with_blockers(gap_preview_env):
    out = _render_gap_preview(
        gap_preview_env, blocking=['point_table', 'group_size_max']
    )

    assert (
        '<h2 style="margin: 0;">Preview: tournament from this request</h2>'
        in out
    )
    assert '20 fields in the create form' in out
    assert 'lt-gap-preview' in out

    assert '<div class="lt-progress" style="margin-top: 12px;">' in out
    assert '<i class="lt-seg-supplied" style="flex: 12;"></i>' in out
    assert '<i class="lt-seg-admin" style="flex: 6;"></i>' in out
    assert '<i class="lt-seg-blocking" style="flex: 2;"></i>' in out

    assert '<div class="legend block">' in out
    assert '<span class="lt-seg-supplied">12 from the request</span>' in out
    assert '<span class="lt-seg-admin">6 still open</span>' in out
    assert '<span class="lt-seg-blocking">2 block creation</span>' in out

    assert '<div class="notification color-danger block">' in out
    assert (
        '<strong>Free-for-All needs points by placement and a max. group '
        'size.</strong>'
    ) in out
    assert (
        'The request supplies neither. Without these values the create form '
        'refuses.'
    ) in out

    assert '<div class="data-label">Still missing (8)</div>' in out
    assert '<tr class="gate">' in out

    # "Prefilled from the request" lists the supplied fields.
    assert (
        '<div class="data-label">Prefilled from the request (12)</div>' in out
    )


def test_view_request_gap_preview_has_no_blocker_notice_without_blockers(
    gap_preview_env,
):
    out = _render_gap_preview(gap_preview_env, blocking=[])

    assert 'notification' not in out
    assert 'Points by placement' not in out
    assert 'class="gate"' not in out
    assert '<span class="lt-seg-blocking">0 block creation</span>' in out
    assert '<i class="lt-seg-blocking"' not in out
    assert 'Still missing (6)' in out


def test_gap_preview_lists_blocking_rows_first_with_the_ffa_tag(
    gap_preview_env,
):
    out = _render_gap_preview(
        gap_preview_env, blocking=['point_table', 'group_size_max']
    )
    missing = out[out.index('Still missing') : out.index('Prefilled from')]

    gate_rows = re.findall(r'<tr class="gate">.*?</tr>', missing, re.DOTALL)
    assert len(gate_rows) == 2
    assert 'Points by placement' in gate_rows[0]
    assert 'Max. group size' in gate_rows[1]
    for row in gate_rows:
        assert '<span class="tag color-danger">Required for FFA</span>' in row

    assert missing.rindex('Required for FFA') < missing.index('by admin')
    assert missing.count('<span class="dimmed">by admin</span>') == 6


def test_gap_preview_admin_fills_end_with_image_url(gap_preview_env):
    """The draft lists the image URL last among the admin fills."""
    out = _render_gap_preview(gap_preview_env, blocking=[])
    missing = out[out.index('Still missing') : out.index('Prefilled from')]

    fields = re.findall(r'<span class="src">(\w+)</span>', missing)
    assert fields == [
        'score_ordering',
        'min_players',
        'min_teams',
        'advancement_count',
        'points_carry_to_losers',
        'image_url',
    ]


def test_gap_preview_uses_the_draft_field_labels(gap_preview_env):
    out = _render_gap_preview(gap_preview_env, blocking=[])

    assert 'Points sorting' in out
    assert 'Advancers per group' in out
    assert 'Points in losers round' in out
    assert 'Tournament mode' in out
    assert 'Tournament start' in out
    assert 'Score ordering' not in out
    assert 'Advance per group' not in out
    assert 'Points carry to losers pool' not in out
    assert 'Elimination mode' not in out


def test_gap_preview_limit_label_follows_the_team_size(gap_preview_env):
    solo = _render_gap_preview(
        gap_preview_env,
        blocking=[],
        tournament_request=_make_request(team_size=1),
    )
    teams = _render_gap_preview(
        gap_preview_env,
        blocking=[],
        tournament_request=_make_request(team_size=3),
    )

    assert '<td class="dimmed">Max. players</td>' in solo
    assert 'Max. teams' not in solo
    assert '<td class="dimmed">Max. teams</td>' in teams
    assert 'Max. players' not in teams


def test_gap_preview_hidden_for_live_created_request(gap_preview_env):
    """AC3: a live `tournament_created` request (terminal, and its
    tournament was never deleted) must not show the create-gap
    preview at all -- the tournament already exists, so there is
    nothing left to prefill or block."""
    out = _render_gap_preview(
        gap_preview_env,
        blocking=[],
        is_terminal=True,
        tournament_deleted=False,
    )

    assert 'Preview: tournament from this request' not in out
    assert 'Still missing' not in out
    assert 'Prefilled from the request' not in out


def test_gap_preview_lists_prefilled_values_and_field_keys(gap_preview_env):
    """AC3: the 'Prefilled from the request' table shows the request's
    actual values, not just the field labels -- and 'Still missing'
    rows show the underlying field key alongside the label, so the
    admin can match it to the create form."""
    tournament_request = _make_request(
        number=142,
        name='Rollator-Rallye 2026',
        game='Mario Kart 8 Deluxe',
        game_format_label='Free-for-All',
        elimination_mode_name='SINGLE_ELIMINATION',
        team_size=1,
        participant_limit=32,
        preferred_start_time=datetime(2026, 10, 3, 14, 0),
    )
    party = SimpleNamespace(title='total-verplant 36')

    out = _render_gap_preview(
        gap_preview_env,
        blocking=[],
        tournament_request=tournament_request,
        party=party,
    )

    # Values, not just labels.
    assert 'Rollator-Rallye 2026' in out
    assert 'Mario Kart 8 Deluxe' in out
    assert 'Free-for-All' in out
    assert 'Single Elimination' in out
    assert 'Solo' in out
    assert '32' in out
    assert 'total-verplant 36' in out
    assert 'Request #0142' in out

    # Field key alongside an admin-fills "Still missing" label.
    assert '<span class="src">score_ordering</span>' in out


def test_gap_preview_marks_preferred_end_as_info_only(gap_preview_env):
    """AC3/Issue-1: `preferred_end_time` has no `create_tournament`
    counterpart -- it must show as a distinct info-only row instead of
    being claimed as "prefilled" alongside the other 12 fields."""
    tournament_request = _make_request(
        preferred_end_time=datetime(2026, 10, 3, 20, 0),
    )

    out = _render_gap_preview(
        gap_preview_env,
        blocking=[],
        tournament_request=tournament_request,
    )

    assert 'Preferred end' in out
    assert 'not a field in the create form' in out
    assert '2026-10-03' in out
    assert '20:00' in out


def test_gap_preview_info_only_row_shares_the_prefilled_table(
    gap_preview_env,
):
    """The info-only row is the last row of the prefilled table."""
    out = _render_gap_preview(gap_preview_env, blocking=[])

    assert out.count('<table class="index') == 2
    split = out.index('Prefilled from the request')
    assert 'preferred_end_time' not in out[:split]
    prefilled = out[split:]
    assert prefilled.count('<table') == 1
    assert prefilled.index('preferred_end_time') > prefilled.index(
        'Request #0142'
    )


# --------------------------------------------------------------------- #
# view_request.html -- elimination mode tile shows a translated label
# (J4)
# --------------------------------------------------------------------- #


@pytest.fixture(scope='module')
def elimination_mode_tile_env():
    src = _VIEW_REQUEST_TEMPLATE.read_text()
    marker = '<div class="data-label">{{ _(\'Tournament mode\') }}</div>'
    start = src.index(marker)
    first_close = src.index('</div>', start)
    end = src.index('</div>', first_close + 1) + len('</div>')
    fragment = src[start:end]

    return _make_env({'elimination_tile': fragment})


def _render_elimination_mode_tile(
    env, *, tournament_request, elimination_mode_labels
):
    tmpl = env.get_template('elimination_tile')
    return tmpl.render(
        tournament_request=tournament_request,
        elimination_mode_labels=elimination_mode_labels,
    )


def test_view_request_elimination_mode_tile_renders_translated_label(
    elimination_mode_tile_env,
):
    """J4: the raw English enum name must not leak -- render the
    translated label the admin forms module already defines for the
    propose/edit dropdowns (`_REQUEST_ELIMINATION_MODE_LABELS`),
    instead of the previous `elimination_mode.name|replace('_',' ')|title`.

    Uses a label deliberately unlike what that old filter would have
    produced for `DOUBLE_ELIMINATION` ("Double Elimination") -- a
    coincidental match there would let this test pass even reverted.
    """
    tournament_request = SimpleNamespace(
        elimination_mode=EliminationMode.DOUBLE_ELIMINATION
    )

    out = _render_elimination_mode_tile(
        elimination_mode_tile_env,
        tournament_request=tournament_request,
        elimination_mode_labels={
            EliminationMode.DOUBLE_ELIMINATION: 'DE Bracket',
        },
    )

    assert 'DE Bracket' in out
    assert 'DOUBLE_ELIMINATION' not in out
    assert 'DOUBLE_ELIMINATION' not in out


def test_view_request_elimination_mode_tile_falls_back_to_raw_name_when_unmapped(
    elimination_mode_tile_env,
):
    """Defensive fallback only: `_REQUEST_ELIMINATION_MODE_LABELS`
    covers every `EliminationMode` member, so a miss should never
    happen in practice -- but render the raw name rather than raising
    under `StrictUndefined` if it ever does."""
    tournament_request = SimpleNamespace(elimination_mode=EliminationMode.NONE)

    out = _render_elimination_mode_tile(
        elimination_mode_tile_env,
        tournament_request=tournament_request,
        elimination_mode_labels={},
    )

    assert 'NONE' in out


def _make_detail_request(
    *,
    description='Three cups, items on.',
    special_rules='Blue shells are banned.',
    desired_template=None,
    notes='Only the orga sees this.',
    **kwargs,
):
    request = _make_request(**kwargs)
    request.description = description
    request.special_rules = special_rules
    request.desired_template = desired_template
    request.notes = notes
    return request


@pytest.fixture(scope='module')
def detail_top_env():
    src = _VIEW_REQUEST_TEMPLATE.read_text()
    start = src.index('{# Six data tiles')
    end = src.index('{# Gap preview: what create_tournament still needs', start)
    fragment = src[start:end]

    env = _make_env({'detail_top': fragment})
    env.filters['dateformat'] = lambda dt, *a, **k: dt.strftime('%Y-%m-%d')
    env.filters['timeformat'] = lambda dt, *a, **k: dt.strftime('%H:%M')
    return env


def _render_detail_top(env, tournament_request):
    return env.get_template('detail_top').render(
        tournament_request=tournament_request,
        elimination_mode_labels=_ELIMINATION_MODE_LABELS_FOR_TESTS,
    )


def test_view_request_facts_are_six_boxes_in_the_draft_order(detail_top_env):
    out = _render_detail_top(
        detail_top_env,
        _make_detail_request(
            team_size=2,
            participant_limit=24,
            preferred_start_time=datetime(2026, 10, 3, 14, 0),
            preferred_end_time=datetime(2026, 10, 3, 20, 0),
        ),
    )
    facts = out[
        out.index('<div class="lt-facts block">') : out.index('</div>\n\n')
    ]

    labels = re.findall(r'<div class="data-label">([^<]+)</div>', facts)
    assert labels == [
        'Game format',
        'Tournament mode',
        'Team size',
        'Participant limit',
        'Preferred start',
        'Preferred end',
    ]
    values = re.findall(r'<div class="data-value">([^<]+)</div>', facts)
    assert values == [
        'Free-for-All',
        'Single Elimination',
        '2',
        '24',
        '2026-10-03, 14:00',
        '2026-10-03, 20:00',
    ]
    assert facts.count('<div class="box">') == 6
    assert 'class="grid' not in out
    assert 'column-cell--grow' not in out
    assert 'Elimination mode' not in out


def test_view_request_text_box_has_description_rules_and_template(
    detail_top_env,
):
    out = _render_detail_top(
        detail_top_env,
        _make_detail_request(desired_template='Bracket 16'),
    )

    labels = re.findall(r'<div class="data-label">([^<]+)</div>', out)
    assert labels[6:] == [
        'Description',
        'Special rules',
        'Desired template',
        'Note to the orga (not public)',
    ]
    assert '<div class="data-label">Game</div>' not in out
    assert 'Three cups, items on.' in out
    assert 'Blue shells are banned.' in out
    assert '<div class="data-value">Bracket 16</div>' in out
    assert 'not specified' not in out


def test_view_request_text_box_dims_missing_rules_and_template(
    detail_top_env,
):
    out = _render_detail_top(
        detail_top_env,
        _make_detail_request(special_rules=None, desired_template=None),
    )

    assert out.count('<div class="data-value dimmed">not specified</div>') == 2
    assert '<div class="data-label">Special rules</div>' in out
    assert '<div class="data-label">Desired template</div>' in out


def test_view_request_orga_note_is_labelled_not_public(detail_top_env):
    out = _render_detail_top(detail_top_env, _make_detail_request())

    assert '<div class="box orga-note block">' in out
    assert '<div class="data-label">Note to the orga (not public)</div>' in out
    assert 'Only the orga sees this.' in out
    assert 'Private note' not in out


def test_view_request_orga_note_is_absent_without_notes(detail_top_env):
    out = _render_detail_top(detail_top_env, _make_detail_request(notes=None))

    assert 'orga-note' not in out
    assert 'Note to the orga' not in out


@pytest.fixture(scope='module')
def page_top_env():
    src = _VIEW_REQUEST_TEMPLATE.read_text()
    start = src.index('<div class="lt-request-page">')
    end = src.index(
        '<div class="row row--space-between row--wrap block"', start
    )
    fragment = src[start:end]

    env = _make_env({'page_top': fragment})
    env.globals['url_for'] = _header_url_for
    return env


def test_view_request_backlink_sits_inside_the_page_wrapper(page_top_env):
    """The backlink is the wrapper's first element."""
    out = page_top_env.get_template('page_top').render(
        party=SimpleNamespace(id='p1')
    )

    assert out.strip() == (
        '<div class="lt-request-page">\n\n'
        '  <a class="lt-backlink" href=".requests_for_party?party_id=p1">'
        '‹ Tournament requests</a>'
    )


def test_view_request_leaves_the_layout_tabs_alone():
    """Overriding `before_body` would replace the layout's main tabs."""
    src = _VIEW_REQUEST_TEMPLATE.read_text()

    assert '{% block before_body %}' not in src
    assert 'render_backlink' not in src
    assert src.index('{% block body %}') < src.index(
        '<div class="lt-request-page">'
    )
    assert src.rindex('</div>') < src.rindex('{%- endblock %}')


def test_view_request_layout_classes_are_defined_in_the_admin_css():
    """Every `lt-` class the markup uses has a rule."""
    src = _VIEW_REQUEST_TEMPLATE.read_text()
    css = _MATCH_ADMIN_CSS.read_text()

    used = {
        name
        for attr in re.findall(r'class="([^"]*)"', src)
        for name in attr.split()
        if name.startswith('lt-')
    }
    assert {
        'lt-request-page',
        'lt-backlink',
        'lt-cols',
        'lt-facts',
        'lt-progress',
        'lt-decision',
    } <= used
    for name in sorted(used - {'lt-gap-preview'}):
        assert f'.{name}' in css, f'.{name} has no rule in {_MATCH_ADMIN_CSS}'


# --------------------------------------------------------------------- #
# view_request.html -- edit button (workspace-dim0.9, compromise C6)
# --------------------------------------------------------------------- #


def _make_header_request(
    *,
    is_editable_by_admin=True,
    status='submitted',
    proposer_id='u1',
    created_at=None,
):
    return SimpleNamespace(
        id='r1',
        number=142,
        name='Some Tournament',
        status=SimpleNamespace(value=status),
        is_editable_by_admin=is_editable_by_admin,
        proposer_id=proposer_id,
        created_at=created_at or datetime(2026, 1, 1, 10, 0),
    )


def _header_url_for(endpoint, **kwargs):
    if not kwargs:
        return endpoint
    qs = '&'.join(f'{k}={v}' for k, v in sorted(kwargs.items()))
    return f'{endpoint}?{qs}'


@pytest.fixture(scope='module')
def header_env():
    src = _VIEW_REQUEST_TEMPLATE.read_text()
    start = src.index('<div class="row row--space-between row--wrap block"')
    end = src.index('<div class="lt-cols">', start)
    fragment = src[start:end]

    template = "{% from 'macros/misc.html' import render_tag %}\n" + fragment

    env = _make_env({'header': template, 'macros/misc.html': _MACROS_MISC})
    env.filters['dateformat'] = lambda dt, *a, **k: dt.strftime('%Y-%m-%d')
    env.filters['timeformat'] = lambda dt, *a, **k: dt.strftime('%H:%M')
    env.globals['url_for'] = _header_url_for
    return env


def _render_header(
    env,
    *,
    tournament_request,
    can_decide,
    proposer=None,
    proposer_name='Proposer',
):
    tmpl = env.get_template('header')
    user = SimpleNamespace(
        has_permission=lambda perm: (
            can_decide and perm == 'lan_tournament.request_decide'
        )
    )
    return tmpl.render(
        tournament_request=tournament_request,
        g=SimpleNamespace(user=user),
        proposer=proposer,
        proposer_name=proposer_name,
    )


def test_view_request_shows_edit_button_when_editable_and_permitted(
    header_env,
):
    out = _render_header(
        header_env,
        tournament_request=_make_header_request(is_editable_by_admin=True),
        can_decide=True,
    )

    assert 'Edit' in out
    assert 'update_request_form' in out


def test_view_request_shows_edit_button_for_accepted_request(header_env):
    """Admins may edit an accepted request too, not only an open one."""
    out = _render_header(
        header_env,
        tournament_request=_make_header_request(
            is_editable_by_admin=True, status='accepted'
        ),
        can_decide=True,
    )

    assert '<span>Edit</span>' in out
    assert 'update_request_form?request_id=r1' in out


def test_view_request_hides_edit_button_without_request_decide_permission(
    header_env,
):
    out = _render_header(
        header_env,
        tournament_request=_make_header_request(is_editable_by_admin=True),
        can_decide=False,
    )

    assert '>Edit<' not in out


def test_view_request_hides_edit_button_when_request_not_editable_by_admin(
    header_env,
):
    out = _render_header(
        header_env,
        tournament_request=_make_header_request(
            is_editable_by_admin=False, status='rejected'
        ),
        can_decide=True,
    )

    assert '>Edit<' not in out


def test_view_request_h1_is_the_name_followed_by_the_status_tag(header_env):
    out = _render_header(
        header_env,
        tournament_request=_make_header_request(status='submitted'),
        can_decide=True,
    )

    h1 = re.search(r'<h1[^>]*>(.*?)</h1>', out, re.DOTALL).group(1)
    assert re.sub(r'\s+', ' ', h1).strip() == (
        'Some Tournament <span class="tag color-warning">Open</span>'
    )
    assert '<small>' not in out
    assert 'Request #0142 by' in out


@pytest.mark.parametrize(
    ('status', 'label', 'colour'),
    [
        ('submitted', 'Open', 'color-warning'),
        ('accepted', 'Accepted', 'color-info'),
        ('tournament_created', 'Tournament created', 'color-success'),
        ('rejected', 'Rejected', 'color-danger'),
        ('withdrawn', 'Withdrawn', 'color-disabled'),
    ],
)
def test_view_request_h1_status_tag_per_status(
    header_env, status, label, colour
):
    out = _render_header(
        header_env,
        tournament_request=_make_header_request(status=status),
        can_decide=True,
    )

    assert f'<span class="tag {colour}">{label}</span>' in out


def test_view_request_header_names_proposer_and_submission_date(
    header_env,
):
    """AC1: the header names the proposer -- linked to their admin
    user page when known -- and the submission date. The page
    previously gave no indication of who submitted the request or
    when."""
    tournament_request = _make_header_request(
        proposer_id='u1', created_at=datetime(2026, 1, 1, 10, 0)
    )

    out = _render_header(
        header_env,
        tournament_request=tournament_request,
        can_decide=True,
        proposer=SimpleNamespace(screen_name='Oma_Gerda'),
        proposer_name='Oma_Gerda',
    )

    assert '#0142' in out
    assert 'Oma_Gerda' in out
    assert 'user_admin.view?user_id=u1' in out
    assert '2026-01-01' in out
    assert '10:00' in out


def test_view_request_header_names_deleted_proposer_as_plain_text(
    header_env,
):
    """When the proposer is unknown (never expected in practice, but
    `proposer` is falsy defensively), the name renders as plain text,
    not as a dangling link."""
    tournament_request = _make_header_request(proposer_id='u404')

    out = _render_header(
        header_env,
        tournament_request=tournament_request,
        can_decide=True,
        proposer=None,
        proposer_name='u404',
    )

    assert 'u404' in out
    assert 'user_admin.view' not in out


# --------------------------------------------------------------------- #
# view_request.html -- proposer_name falls back for a deleted proposer
# (G5, bead workspace-c9o4.3)
# --------------------------------------------------------------------- #


@pytest.fixture(scope='module')
def proposer_name_env():
    src = _VIEW_REQUEST_TEMPLATE.read_text()
    start = src.index('{% block body %}') + len('{% block body %}')
    end = src.index('<div class="lt-request-page">', start)
    fragment = src[start:end]

    # `proposer_name` is `{%- set %}` at template scope, so append an
    # output right in the same fragment to observe it -- it is never
    # printed on its own in the real template until much further down.
    template = fragment + '{{ proposer_name }}'

    return _make_env({'proposer_name': template})


def _render_proposer_name(env, *, tournament_request, users_by_id):
    tmpl = env.get_template('proposer_name')
    return tmpl.render(
        tournament_request=tournament_request, users_by_id=users_by_id
    )


def test_view_request_proposer_name_falls_back_for_deleted_proposer(
    proposer_name_env,
):
    """G5: a deleted user's `screen_name` is `None`, and `proposer` is
    still found in `users_by_id` -- printing it directly used to render
    the literal text "None" instead of any indication the account is
    gone."""
    tournament_request = _make_request(proposer_id='u1')
    out = _render_proposer_name(
        proposer_name_env,
        tournament_request=tournament_request,
        users_by_id={'u1': SimpleNamespace(screen_name=None, deleted=True)},
    )

    assert out.strip() == 'Deleted user'
    assert 'None' not in out


def test_view_request_proposer_name_shows_screen_name_when_present(
    proposer_name_env,
):
    tournament_request = _make_request(proposer_id='u1')
    out = _render_proposer_name(
        proposer_name_env,
        tournament_request=tournament_request,
        users_by_id={
            'u1': SimpleNamespace(screen_name='Alice', deleted=False)
        },
    )

    assert out.strip() == 'Alice'


def test_view_request_proposer_name_falls_back_to_id_when_proposer_missing(
    proposer_name_env,
):
    """The pre-existing fallback for a proposer ID that resolves to no
    user at all (never observed in practice) must keep working exactly
    as before this fix."""
    tournament_request = _make_request(proposer_id='u404')
    out = _render_proposer_name(
        proposer_name_env,
        tournament_request=tournament_request,
        users_by_id={},
    )

    assert out.strip() == 'u404'


_ADMIN_MAIN_TABS_MACROS = pathlib.Path(
    'byceps/services/core/blueprints/admin/templates/macros/admin.html'
).read_text()

_ICONS_STUB_FOR_NAV = (
    '{% macro render_icon(name, color=None, title=None, '
    "filename='icons') %}{% endmacro %}"
)


@pytest.fixture(scope='module')
def nav_tab_label_env():
    src = _LAYOUT_TEMPLATE.read_text()
    start = src.index('{% block before_body %}') + len(
        '{% block before_body %}'
    )
    end = src.index('{%- endblock %}', start)
    fragment = src[start:end]

    template = (
        "{% from 'macros/admin.html' import render_main_tabs %}\n"
        "{% from 'macros/misc.html' import render_tag %}\n" + fragment
    )

    return Environment(
        undefined=StrictUndefined,
        autoescape=True,
        loader=DictLoader(
            {
                'nav': template,
                'macros/admin.html': _ADMIN_MAIN_TABS_MACROS,
                'macros/misc.html': _MACROS_MISC,
                'macros/icons.html': _ICONS_STUB_FOR_NAV,
            }
        ),
    )


def _render_nav_tab_label(
    env, app, *, request_count, current_tab='tournaments'
):
    tmpl = env.get_template('nav')
    with app.test_request_context('/'):
        # `Navigation` reads the real Flask `g`: a request context is needed.
        flask_g.user = SimpleNamespace(has_permission=lambda perm: True)
        return tmpl.render(
            _=lambda s, **kw: (s % kw) if kw else s,
            url_for=lambda endpoint, **kw: endpoint,
            current_page_party=SimpleNamespace(id='p1'),
            current_tab=current_tab,
            Navigation=Navigation,
            lan_tournament_pending_request_count=lambda party_id: request_count,
        )


def test_nav_tab_label_plain_without_open_requests(
    nav_tab_label_env, minimal_app
):
    """Without open requests the Requests tab shows the plain label."""
    out = _render_nav_tab_label(nav_tab_label_env, minimal_app, request_count=0)

    assert 'Tournament requests' in out
    assert '<span class="tag' not in out


def test_nav_tab_label_carries_count_tag_exactly_once(
    nav_tab_label_env, minimal_app
):
    """With open requests the count tag is rendered once, unescaped."""
    out = _render_nav_tab_label(nav_tab_label_env, minimal_app, request_count=3)

    assert out.count('<span class="tag color-warning">3</span>') == 1
    assert '&lt;span' not in out
    assert 'Tournament requests <span class="tag color-warning">3</span>' in out


# --------------------------------------------------------------------- #
# requests_for_party.html -- mixed queue
# --------------------------------------------------------------------- #


@pytest.fixture(scope='module')
def queue_env():
    src = _QUEUE_TEMPLATE.read_text()
    start = src.index('{% block body %}') + len('{% block body %}')
    end = src.rindex('{%- endblock %}')
    fragment = src[start:end]

    template = (
        "{% from 'macros/admin.html' import render_extra_in_heading %}\n"
        "{% from 'macros/misc.html' import render_tag %}\n" + fragment
    )

    env = _make_env(
        {
            'queue': template,
            'macros/misc.html': _MACROS_MISC,
            'macros/admin.html': _MACROS_ADMIN,
        }
    )
    env.filters['dateformat'] = lambda dt, *a, **k: dt.strftime('%Y-%m-%d')
    env.filters['timeformat'] = lambda dt, *a, **k: dt.strftime('%H:%M')
    return env


def _render_queue(
    env,
    *,
    pending_requests,
    done_requests,
    gaps_by_request_id,
    stale_request_ids=frozenset(),
    users_by_id=None,
    elimination_mode_labels=None,
    can_create=True,
    can_decide=True,
    status_filter=None,
    status_counts=None,
):
    tmpl = env.get_template('queue')
    all_requests = pending_requests + done_requests
    if users_by_id is None:
        users_by_id = {
            'u1': SimpleNamespace(screen_name='ProposerOne'),
            'u2': SimpleNamespace(screen_name='ProposerTwo'),
        }
    if elimination_mode_labels is None:
        elimination_mode_labels = _ELIMINATION_MODE_LABELS_FOR_TESTS
    if status_counts is None:
        status_counts = {r.status.value: 1 for r in all_requests}
    user = SimpleNamespace(
        has_permission=lambda perm: (
            (can_create and perm == 'lan_tournament.create')
            or (can_decide and perm == 'lan_tournament.request_decide')
        )
    )
    return tmpl.render(
        party=SimpleNamespace(id='p1', title='Some Party'),
        pending_requests=pending_requests,
        done_requests=done_requests,
        stale_request_ids=stale_request_ids,
        gaps_by_request_id=gaps_by_request_id,
        users_by_id=users_by_id,
        status_counts=status_counts,
        status_filter=status_filter,
        total_count=len(all_requests),
        elimination_mode_labels=elimination_mode_labels,
        g=SimpleNamespace(user=user),
    )


def test_requests_for_party_renders_mixed_queue(queue_env):
    submitted = _make_request(
        request_id='r1', number=142, proposer_id='u1', status='submitted'
    )
    accepted = _make_request(
        request_id='r2', number=7, proposer_id='u2', status='accepted'
    )
    tournament_created = _make_request(
        request_id='r3', number=3, status='tournament_created'
    )

    out = _render_queue(
        queue_env,
        pending_requests=[submitted, accepted],
        done_requests=[tournament_created],
        gaps_by_request_id={
            'r1': _make_gap(blocking=['point_table', 'group_size_max']),
            'r2': _make_gap(blocking=[]),
        },
    )

    # Both sections render with their headers.
    assert 'Decision pending' in out
    assert 'Resolved' in out

    # Pending rows are visually split by status. Exact class match --
    # a substring check would pass against a typo like "is-opened" too.
    assert 'class="is-open"' in out
    assert 'class="is-accepted"' in out

    # Proposer names resolved, not raw IDs.
    assert 'ProposerOne' in out
    assert 'ProposerTwo' in out

    # Action buttons differ by status.
    assert 'Review' in out
    assert 'Create tournament' in out

    # The submitted request's FFA gap is visible one screen earlier than
    # the detail view.
    assert 'required fields are missing' in out

    # The accepted request has no blocking gap, so no missing-fields line
    # is attached to it. The submitted request's own note renders twice
    # overall -- once in the desktop table, once in the mobile itemlist
    # that mirrors the same pending requests below 47rem.
    assert out.count('required fields are missing') == 2

    # Done section renders the terminal status.
    assert 'Tournament created' in out

    # Design columns (Issue 4): the queue table carries Limit and
    # Preferred period alongside the pre-existing Mode/Status/Submitted.
    assert 'Limit' in out
    assert 'Preferred period' in out

    # The accepted row's queue action creates the tournament directly,
    # with no success-green styling (that color is reserved for the
    # confirmed "Tournament created" status elsewhere in the queue).
    assert (
        '<a class="button is-compact" href=".create_form">'
        'Create tournament</a>' in out
    )

    # Both a desktop table and a mobile itemlist render the same
    # pending/resolved rows -- the CSS (not markup presence) decides
    # which one is visible at a given viewport width.
    assert 'lt-requests-table' in out
    assert '<ol class="itemlist lt-requests block">' in out


def test_requests_for_party_shows_deleted_user_for_deleted_proposer(
    queue_env,
):
    """G5: a deleted proposer's `screen_name` is `None` in both the
    pending and the resolved table -- printing it directly used to
    render the literal text "None"."""
    pending = _make_request(
        request_id='r1', number=142, proposer_id='u1', status='submitted'
    )
    done = _make_request(
        request_id='r2', number=7, proposer_id='u1', status='rejected'
    )

    out = _render_queue(
        queue_env,
        pending_requests=[pending],
        done_requests=[done],
        gaps_by_request_id={'r1': _make_gap(blocking=[])},
        users_by_id={'u1': SimpleNamespace(screen_name=None, deleted=True)},
    )

    # Once each in the desktop table and once each in the mirrored
    # mobile itemlist, for both the pending and the resolved row.
    assert out.count('Deleted user') == 4
    assert 'None' not in out


def test_requests_for_party_renders_empty_queue(queue_env):
    out = _render_queue(
        queue_env, pending_requests=[], done_requests=[], gaps_by_request_id={}
    )

    assert 'No tournament requests are awaiting a decision.' in out
    assert 'No requests have been decided yet.' in out


# --------------------------------------------------------------------- #
# --------------------------------------------------------------------- #


def test_requests_for_party_has_no_stale_markers_regardless_of_stale_ids(
    queue_env,
):
    """The stale-accepted warning and the red tag are gone."""
    accepted = _make_request(
        request_id='r1', number=7, proposer_id='u1', status='accepted'
    )

    out = _render_queue(
        queue_env,
        pending_requests=[accepted],
        done_requests=[],
        gaps_by_request_id={'r1': _make_gap(blocking=[])},
        stale_request_ids={'r1'},
    )

    assert 'class="is-accepted"' in out
    assert 'is-stale' not in out
    assert 'No tournament yet' not in out
    assert 'notification' not in out


def test_requests_for_party_accepted_row_shows_blue_note_on_mobile_only(
    queue_env,
):
    """The mobile accepted row carries the blue accepted note."""
    accepted = _make_request(
        request_id='r1', number=7, proposer_id='u1', status='accepted'
    )
    submitted = _make_request(
        request_id='r2', number=8, proposer_id='u1', status='submitted'
    )

    out = _render_queue(
        queue_env,
        pending_requests=[accepted, submitted],
        done_requests=[],
        gaps_by_request_id={
            'r1': _make_gap(blocking=[]),
            'r2': _make_gap(blocking=[]),
        },
    )

    assert out.count('class="details lt-accepted-note"') == 1
    assert 'Accepted · tournament not created yet' in out


# --------------------------------------------------------------------- #
# requests_for_party.html -- queue create button gating (J1)
# --------------------------------------------------------------------- #


def test_requests_for_party_shows_create_button_with_both_permissions(
    queue_env,
):
    accepted = _make_request(
        request_id='r1', number=7, proposer_id='u1', status='accepted'
    )

    out = _render_queue(
        queue_env,
        pending_requests=[accepted],
        done_requests=[],
        gaps_by_request_id={'r1': _make_gap(blocking=[])},
        can_create=True,
        can_decide=True,
    )

    assert 'Create tournament' in out


def test_requests_for_party_hides_create_button_without_request_decide(
    queue_env,
):
    """J1: `lan_tournament.create` alone must not show the button --
    submitting it would only be refused (and clear the hidden link) by
    `create`'s own from-request gate, which now also requires
    `request_decide`."""
    accepted = _make_request(
        request_id='r1', number=7, proposer_id='u1', status='accepted'
    )

    out = _render_queue(
        queue_env,
        pending_requests=[accepted],
        done_requests=[],
        gaps_by_request_id={'r1': _make_gap(blocking=[])},
        can_create=True,
        can_decide=False,
    )

    assert 'Create tournament' not in out


def test_requests_for_party_hides_create_button_without_create_permission(
    queue_env,
):
    accepted = _make_request(
        request_id='r1', number=7, proposer_id='u1', status='accepted'
    )

    out = _render_queue(
        queue_env,
        pending_requests=[accepted],
        done_requests=[],
        gaps_by_request_id={'r1': _make_gap(blocking=[])},
        can_create=False,
        can_decide=True,
    )

    assert 'Create tournament' not in out


# --------------------------------------------------------------------- #
# requests_for_party.html -- translated elimination mode labels (J4)
# --------------------------------------------------------------------- #


def test_requests_for_party_shows_translated_elimination_mode_labels(
    queue_env,
):
    submitted = _make_request(
        request_id='r1',
        number=142,
        proposer_id='u1',
        status='submitted',
        elimination_mode_name='DOUBLE_ELIMINATION',
    )
    done = _make_request(
        request_id='r2',
        number=3,
        proposer_id='u1',
        status='rejected',
        elimination_mode_name='ROUND_ROBIN',
    )

    # Deliberately unlike what the old `.name|replace('_',' ')|title`
    # filter would have produced for these two modes ("Double
    # Elimination"/"Round Robin") -- a coincidental match there would
    # let this test pass even reverted.
    out = _render_queue(
        queue_env,
        pending_requests=[submitted],
        done_requests=[done],
        gaps_by_request_id={'r1': _make_gap(blocking=[])},
        elimination_mode_labels={
            EliminationMode.DOUBLE_ELIMINATION: 'DE Bracket',
            EliminationMode.ROUND_ROBIN: 'RR League',
        },
    )

    assert 'DE Bracket' in out
    assert 'RR League' in out
    assert 'DOUBLE_ELIMINATION' not in out
    assert 'ROUND_ROBIN' not in out


# --------------------------------------------------------------------- #
# lan_tournament_match_admin.css -- request design hooks (Issue 4)
# --------------------------------------------------------------------- #


def test_admin_request_css_defines_design_hooks():
    """Every selector Implementation §4 names for the request queue and
    detail hooks must actually be defined -- the markup alone (`is-open`,
    `orga-note`, `tr.gate`, ...) renders unstyled otherwise."""
    css = _MATCH_ADMIN_CSS.read_text()

    for hook in (
        'tr.is-open',
        'tr.is-accepted',
        'tr.is-stale',
        'li.is-open',
        'li.is-accepted',
        '.details.lt-gap-note',
        '.box.orga-note',
        'tr.gate',
        '.src',
        '.legend',
        'details.rej',
        '.sep-line',
        '.lt-wide',
        '.lt-request-stack',
        '.lt-request-filter',
        '.lt-requests-table',
        'ol.itemlist.lt-requests',
    ):
        assert hook in css, f'{hook!r} not defined in {_MATCH_ADMIN_CSS}'

    # The hooks resolve through the real admin design tokens, not
    # hard-coded colours -- a token typo would silently fall back to
    # nothing since this file defines no `var(..., fallback)` here.
    for token in (
        '--color-warning',
        '--color-info',
        '--color-danger',
        '--dimmed-color',
    ):
        assert token in css, f'{token!r} not used in {_MATCH_ADMIN_CSS}'


def test_request_templates_link_admin_css():
    """Both request templates must link the stylesheet that defines
    their hooks -- one linking it and the other not would leave half
    the surface unstyled."""
    for template_path in (_VIEW_REQUEST_TEMPLATE, _QUEUE_TEMPLATE):
        src = template_path.read_text()
        assert 'lan_tournament_match_admin.css' in src, (
            f'{template_path} does not link the admin request stylesheet'
        )


# --------------------------------------------------------------------- #
# requests_for_party.html -- design queue columns and filter (Issue 4)
# --------------------------------------------------------------------- #


def test_queue_renders_design_columns(queue_env):
    submitted = _make_request(
        request_id='r1',
        number=142,
        proposer_id='u1',
        status='submitted',
        game='Mario Kart 8 Deluxe',
        participant_limit=32,
        preferred_start_time=datetime(2026, 10, 3, 14, 0),
        preferred_end_time=datetime(2026, 10, 3, 20, 0),
        created_at=datetime(2026, 9, 26, 14, 2),
    )

    out = _render_queue(
        queue_env,
        pending_requests=[submitted],
        done_requests=[],
        gaps_by_request_id={'r1': _make_gap(blocking=[])},
    )

    # New headers the draft adds beside the pre-existing Name/Proposer/
    # Mode/Status.
    assert '<th class="number">Limit</th>' in out
    assert '<th>Preferred period</th>' in out
    assert '<th>Submitted</th>' in out

    assert '<span class="details">Mario Kart 8 Deluxe</span>' in out
    assert '#0142' not in out

    # Limit, period and submission date are rendered as real values,
    # not left for the admin to look up on the detail page.
    assert '<td class="number">32</td>' in out
    assert '2026-10-03' in out
    assert '2026-09-26' in out


def test_queue_filter_tags_use_status_colours(queue_env):
    out = _render_queue(
        queue_env, pending_requests=[], done_requests=[], gaps_by_request_id={}
    )

    assert 'lt-request-filter' in out

    # The "All" tag uses the neutral `lt-all` hook, not a core colour class.
    assert 'class="tag lt-all">All' in out
    assert 'class="tag color-warning">Open' in out
    assert 'class="tag color-info">Accepted' in out
    assert 'class="tag color-success">Tournament created' in out
    assert 'class="tag color-danger">Rejected' in out
    assert 'class="tag color-disabled">Withdrawn' in out

    # With no filter applied, only the "All" link is marked current.
    assert out.count('aria-current="page"') == 1
    all_link_start = out.index('requests_for_party')
    assert 'aria-current="page"' in out[all_link_start : all_link_start + 80]


def test_queue_filter_active_status_gets_aria_current(queue_env):
    out = _render_queue(
        queue_env,
        pending_requests=[],
        done_requests=[],
        gaps_by_request_id={},
        status_filter=TournamentRequestStatus.accepted,
        status_counts={'accepted': 2},
    )

    # Exactly one active link -- the Accepted tag -- not the "All" tag.
    assert out.count('aria-current="page"') == 1
    accepted_pos = out.index('color-info">Accepted')
    preceding = out[max(0, accepted_pos - 200) : accepted_pos]
    assert 'aria-current="page"' in preceding


def test_queue_gap_note_uses_danger_details_class(queue_env):
    submitted = _make_request(
        request_id='r1', number=142, proposer_id='u1', status='submitted'
    )

    out = _render_queue(
        queue_env,
        pending_requests=[submitted],
        done_requests=[],
        gaps_by_request_id={
            'r1': _make_gap(blocking=['point_table', 'group_size_max'])
        },
    )

    # Once in the desktop table's Mode cell, once more in the mirrored
    # mobile itemlist -- never the bare `.details` class for this note,
    # which would lose the CSS hook's danger colouring entirely.
    assert out.count('class="details lt-gap-note"') == 2
    assert 'required fields are missing' in out


def test_queue_create_action_is_plain_compact_button(queue_env):
    accepted = _make_request(
        request_id='r1', number=7, proposer_id='u1', status='accepted'
    )

    out = _render_queue(
        queue_env,
        pending_requests=[accepted],
        done_requests=[],
        gaps_by_request_id={'r1': _make_gap(blocking=[])},
    )

    # `color-success` stays reserved for the confirmed "Tournament
    # created" status tag (also present, in the always-rendered status
    # filter bar) -- the queue action link itself carries no colour.
    assert (
        '<a class="button is-compact" href=".create_form">'
        'Create tournament</a>' in out
    )
    assert 'button is-compact color-success' not in out
    assert 'color-success" href=".create_form"' not in out


def test_queue_renders_mobile_itemlist(queue_env):
    submitted = _make_request(
        request_id='r1', number=142, proposer_id='u1', status='submitted'
    )
    tournament_created = _make_request(
        request_id='r2', number=3, status='tournament_created'
    )

    out = _render_queue(
        queue_env,
        pending_requests=[submitted],
        done_requests=[tournament_created],
        gaps_by_request_id={'r1': _make_gap(blocking=[])},
    )

    # A mobile itemlist follows both the pending and the resolved
    # table -- the media queries (not markup presence) pick one.
    assert out.count('<ol class="itemlist lt-requests') == 2
    assert 'li-top' in out
    assert 'class="dimmed">Some Game ·' in out


def test_queue_filter_status_label_uses_lbl_and_dimmed_classes(queue_env):
    """The filter bar's caption carries the `lbl` and `dimmed` classes."""
    out = _render_queue(
        queue_env, pending_requests=[], done_requests=[], gaps_by_request_id={}
    )

    assert '<span class="lbl dimmed">Status:</span>' in out


def test_queue_mode_label_omits_slash_for_none_elimination_mode(queue_env):
    """A Highscore request shows the bare format label without a mode."""
    highscore_request = _make_request(
        request_id='r1',
        proposer_id='u1',
        status='submitted',
        game_format_label='Highscore',
        elimination_mode_name='NONE',
    )

    out = _render_queue(
        queue_env,
        pending_requests=[highscore_request],
        done_requests=[],
        gaps_by_request_id={'r1': _make_gap(blocking=[])},
        elimination_mode_labels={},
    )

    assert out.count('Highscore') == 2
    assert 'Highscore /' not in out
    assert 'NONE' not in out


def test_queue_mobile_details_shows_abbreviated_participant_count(queue_env):
    """The mobile itemlist abbreviates the participant limit."""
    submitted = _make_request(
        request_id='r1',
        proposer_id='u1',
        status='submitted',
        participant_limit=32,
    )

    out = _render_queue(
        queue_env,
        pending_requests=[submitted],
        done_requests=[],
        gaps_by_request_id={'r1': _make_gap(blocking=[])},
    )

    assert '32 part.' in out
    assert 'submitted 2025-12-20' in out


def test_queue_period_details_use_short_end_date_pattern(queue_env):
    """The period column shows the end time without a year."""
    src = _QUEUE_TEMPLATE.read_text()
    assert "preferred_end_time|dateformat('dd.MM.')" in src


# --------------------------------------------------------------------- #
# create_form.html -- from_request_id hidden field
# --------------------------------------------------------------------- #
#
# A rendered hidden field is exactly the kind of thing that looks right
# and carries nothing: this exercises the real `TournamentCreateForm`
# (built inside a real Flask app/request context, since `LocalizedForm`
# needs `current_app.config['LOCALE']`) through the real WTForms field
# renderer, not a stub -- so a missing/renamed `name=`/`value=` attribute
# would actually be caught.


@pytest.fixture(scope='module')
def minimal_app():
    from flask import Flask
    from flask_babel import Babel

    a = Flask(__name__)
    a.config['TESTING'] = True
    a.config['LOCALE'] = 'en'
    a.config['BABEL_DEFAULT_LOCALE'] = 'en'
    # `TournamentCreateForm` needs only `LOCALE` above, but
    # `TournamentRequestUpdateForm.set_format_choices` eagerly
    # `str()`s a `lazy_gettext` reason string, which needs a live
    # Babel extension instance (`app.extensions['babel']`) or raises
    # `KeyError` rather than merely misbehaving.
    Babel(a)
    return a


@pytest.fixture(scope='module')
def create_form_hidden_field_env():
    src = _CREATE_FORM_TEMPLATE.read_text()
    marker = '{{ form.from_request_id() }}'
    start = src.index(
        '<form action="{{ url_for(\'.create\', party_id=party.id) }}"'
    )
    end = src.index(marker) + len(marker)
    fragment = src[start:end]

    return _make_env({'create_form_fragment': fragment})


def test_create_form_renders_from_request_hidden_field(
    create_form_hidden_field_env, minimal_app
):
    from byceps.services.lan_tournament.blueprints.admin.forms import (
        TournamentCreateForm,
    )

    request_id = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'

    with minimal_app.app_context(), minimal_app.test_request_context('/'):
        form = TournamentCreateForm()
        form.from_request_id.data = request_id

        tmpl = create_form_hidden_field_env.get_template('create_form_fragment')
        out = tmpl.render(party=SimpleNamespace(id='p1'), form=form)

    assert 'type="hidden"' in out
    assert 'name="from_request_id"' in out
    assert f'value="{request_id}"' in out


def test_create_form_hidden_field_is_empty_without_a_prefill(
    create_form_hidden_field_env, minimal_app
):
    """A plain create (no `?from_request=`) must not leak a stale value."""
    from byceps.services.lan_tournament.blueprints.admin.forms import (
        TournamentCreateForm,
    )

    with minimal_app.app_context(), minimal_app.test_request_context('/'):
        form = TournamentCreateForm()

        tmpl = create_form_hidden_field_env.get_template('create_form_fragment')
        out = tmpl.render(party=SimpleNamespace(id='p1'), form=form)

    assert 'type="hidden"' in out
    assert 'name="from_request_id"' in out
    assert 'value=""' in out


# --------------------------------------------------------------------- #
# create_form.html -- provenance banner and request back link
# (workspace-pv3b.2, defect workspace-hm1a)
# --------------------------------------------------------------------- #

_MACROS_ADMIN_BACKLINK = """
{% macro render_backlink(url, label) -%}
<a class="backlink" href="{{ url }}">{{ label }}</a>
{%- endmacro %}
"""


@pytest.fixture(scope='module')
def create_form_banner_env():
    src = _CREATE_FORM_TEMPLATE.read_text()

    before_body_start = src.index('{% block before_body %}') + len(
        '{% block before_body %}'
    )
    before_body_end = src.index('{%- endblock %}', before_body_start)
    backlink_fragment = (
        "{% from 'macros/admin.html' import render_backlink %}"
        + src[before_body_start:before_body_end]
    )

    banner_start = src.index('{# Provenance banner')
    banner_end = src.index('<form action=', banner_start)
    banner_fragment = src[banner_start:banner_end]

    env = _make_env(
        {
            'create_form_backlink': backlink_fragment,
            'create_form_banner': banner_fragment,
            'macros/admin.html': _MACROS_ADMIN_BACKLINK,
        }
    )
    env.filters['dateformat'] = lambda dt, *a, **k: dt.strftime('%Y-%m-%d')
    env.filters['timeformat'] = lambda dt, *a, **k: dt.strftime('%H:%M')
    return env


def _render_create_form_backlink(env, *, party, source_request):
    tmpl = env.get_template('create_form_backlink')
    return tmpl.render(party=party, source_request=source_request)


def _render_create_form_banner(
    env,
    *,
    source_request,
    source_proposer_name,
    source_request_blocking_field_labels,
):
    tmpl = env.get_template('create_form_banner')
    return tmpl.render(
        source_request=source_request,
        source_proposer_name=source_proposer_name,
        source_request_blocking_field_labels=(
            source_request_blocking_field_labels
        ),
    )


def test_create_form_renders_provenance_banner_and_request_backlink(
    create_form_banner_env,
):
    """A `source_request` prefill draws a back link to the request and
    a banner naming what was prefilled, the info-only preferred end,
    and (only when the gap has blocking fields) what still blocks it."""
    party = SimpleNamespace(id='p1')
    source_request = SimpleNamespace(
        id='r1',
        number=142,
        team_size=2,
        participant_limit=12,
        preferred_end_time=datetime(2026, 10, 3, 20, 0),
    )

    backlink_out = _render_create_form_backlink(
        create_form_banner_env, party=party, source_request=source_request
    )

    assert 'href=".view_request"' in backlink_out
    assert 'Tournament request #0142' in backlink_out
    assert '.index' not in backlink_out

    banner_out = _render_create_form_banner(
        create_form_banner_env,
        source_request=source_request,
        source_proposer_name='Oma_Gerda',
        source_request_blocking_field_labels=[],
    )

    assert 'notification color-info' in banner_out
    assert 'Prefilled from request #0142 by Oma_Gerda.' in banner_out
    assert 'Team size: 2, limit: 12.' in banner_out
    assert 'Preferred end (not a field here):' in banner_out
    assert '2026-10-03' in banner_out
    assert '20:00' in banner_out
    assert 'Still missing' not in banner_out

    blocking_out = _render_create_form_banner(
        create_form_banner_env,
        source_request=source_request,
        source_proposer_name=None,
        source_request_blocking_field_labels=[
            'Points by placement',
            'Max. group size',
        ],
    )

    assert 'Prefilled from request #0142 by Unknown.' in blocking_out
    assert (
        'Still missing before this request can become a tournament: '
        'Points by placement, Max. group size.' in blocking_out
    )


def test_create_form_renders_without_source_request(create_form_banner_env):
    """A plain create (no request behind it) shows the ordinary
    Tournaments back link and no provenance banner at all."""
    party = SimpleNamespace(id='p1')

    backlink_out = _render_create_form_backlink(
        create_form_banner_env, party=party, source_request=None
    )

    assert 'href=".index"' in backlink_out
    assert 'Tournaments' in backlink_out
    assert 'Tournament request' not in backlink_out

    banner_out = _render_create_form_banner(
        create_form_banner_env,
        source_request=None,
        source_proposer_name=None,
        source_request_blocking_field_labels=[],
    )

    assert 'notification color-info' not in banner_out
    assert banner_out.strip() == ''


# --------------------------------------------------------------------- #
# update_request_form.html -- admin request edit (workspace-dim0.9)
# --------------------------------------------------------------------- #
#
# Like `create_form_hidden_field_env` above, this exercises the real
# `TournamentRequestUpdateForm`/WTForms field rendering (not a
# `SimpleNamespace` stub) through the real `macros/forms.html`, since
# the JS hook contract (see the epic's Wave 6 lead addendum) lives in
# per-field `**kwargs` that a stub `form_field` would just swallow.


def _extract_block(src: str, block_name: str) -> str:
    """Return one top-level `{% block name %}...{%- endblock %}`'s body.

    Plain string search, not a Jinja parse -- same approach as the
    `gap_preview` fragment above. `update_request_form.html` has a
    `body` block followed by a `scripts` block, so (unlike
    `_VIEW_REQUEST_TEMPLATE`/`_QUEUE_TEMPLATE`, which have no trailing
    block) this can't reuse `str.rindex('{%- endblock %}')` to grab
    "everything up to the last endblock": that would leave `body`'s
    own closing tag and the `scripts` block's opening tag as unmatched
    literal Jinja tags in the middle of the fragment. Extracting each
    block by name and concatenating the two fragments in Python
    sidesteps that -- the assembled test template has zero `block`/
    `endblock` tokens left in it.
    """
    start_tag = f'{{% block {block_name} %}}'
    start = src.index(start_tag) + len(start_tag)
    end = src.index('{%- endblock %}', start)
    return src[start:end]


_FORMS_MACRO_SRC = pathlib.Path(
    'byceps/services/core/blueprints/common/templates/macros/forms.html'
).read_text()

_ICONS_STUB = (
    '{% macro render_icon(name, color=None, title=None, '
    "filename='icons') %}{% endmacro %}"
)


def _update_request_form_url_for(endpoint, **kwargs):
    if endpoint == 'static':
        return '/static/' + kwargs.get('filename', '')
    return '/' + endpoint.lstrip('.')


@pytest.fixture(scope='module')
def update_request_form_env():
    src = _UPDATE_REQUEST_FORM_TEMPLATE.read_text()
    body = _extract_block(src, 'body')
    scripts = _extract_block(src, 'scripts')
    template = (
        "{% from 'macros/forms.html' import form_field, form_field_errors %}\n"
        "{% from 'macros/misc.html' import render_tag %}\n"
        + body
        + '\n'
        + scripts
    )

    e = Environment(
        undefined=StrictUndefined,
        autoescape=True,
        loader=DictLoader(
            {
                'update_request_form': template,
                'macros/forms.html': _FORMS_MACRO_SRC,
                'macros/icons.html': _ICONS_STUB,
                'macros/misc.html': _MACROS_MISC,
            }
        ),
    )
    e.globals['_'] = lambda s, **kw: (s % kw) if kw else s
    e.globals['url_for'] = _update_request_form_url_for
    return e


def _render_update_request_form(
    env,
    app,
    *,
    party_capacity,
    team_size=1,
    status='submitted',
    proposer_known=True,
    proposer_name='Oma_Gerda',
):
    from byceps.services.lan_tournament.blueprints.admin.forms import (
        TournamentRequestUpdateForm,
    )
    from byceps.services.lan_tournament.models.elimination_mode import (
        EliminationMode,
    )
    from byceps.services.lan_tournament.models.game_format import GameFormat

    with app.app_context(), app.test_request_context('/'):
        form = TournamentRequestUpdateForm(
            data={
                'name': 'Some Tournament',
                'game': 'Some Game',
                'game_format': GameFormat.ONE_V_ONE.value,
                'elimination_mode': EliminationMode.SINGLE_ELIMINATION.value,
                'team_size': team_size,
                'participant_limit': 16,
                'preferred_start_time': datetime(2026, 1, 1, 10, 0),
                'preferred_end_time': datetime(2026, 1, 1, 18, 0),
                'description': 'A description',
                'special_rules': '',
                'notes': '',
                'desired_template': '',
            }
        )
        form.set_format_choices()

        tmpl = env.get_template('update_request_form')
        return tmpl.render(
            form=form,
            tournament_request=SimpleNamespace(
                id='r1',
                number=142,
                name='Some Tournament',
                proposer_id='u1',
                status=SimpleNamespace(value=status),
            ),
            party_capacity=party_capacity,
            users_by_id=(
                {'u1': SimpleNamespace(screen_name=proposer_name)}
                if proposer_known
                else {}
            ),
            proposer_name=proposer_name,
        )


def test_update_request_form_renders(update_request_form_env, minimal_app):
    """The admin edit form renders under `StrictUndefined`."""
    out = _render_update_request_form(
        update_request_form_env, minimal_app, party_capacity=240
    )

    # NONE is invalid for the selected ONE_V_ONE format -- rendered
    # disabled, never hidden. Match the option by its `value`, not by
    # `html_params` sorts `data-reasons`/`data-label-base` before `disabled`.
    none_option = re.search(r'<option[^>]*value="NONE"[^>]*>', out)
    assert none_option, 'no <option value="NONE"> in output'
    assert 'disabled' in none_option.group(0)

    # The JS hook script tag renders after the form.
    form_index = out.index('<form')
    script_index = out.index('src="/static/behavior/lan_tournament_request.js"')
    assert script_index > form_index
    assert '%(' not in out


def test_update_request_form_wraps_the_page_like_the_draft(
    update_request_form_env, minimal_app
):
    """`.lt-edit` sits inside `.lt-request-page`, and the backlink inside both."""
    out = _render_update_request_form(
        update_request_form_env, minimal_app, party_capacity=240
    )

    assert re.search(
        r'<div class="lt-request-page">\s*<div class="lt-edit">\s*'
        r'<a class="lt-backlink" href="/view_request">‹ Some Tournament</a>',
        out,
    )
    assert 'lt-request-page lt-edit' not in out


def test_update_request_form_header_names_status_and_proposer(
    update_request_form_env, minimal_app
):
    out = _render_update_request_form(
        update_request_form_env, minimal_app, party_capacity=240
    )

    header = re.search(r'<h1 class="title"[^>]*>(.*?)</h1>', out, re.DOTALL)
    assert header
    assert 'Edit request' in header.group(1)
    assert '<span class="tag color-warning">Open</span>' in header.group(1)
    meta = re.search(
        r'<div class="dimmed"[^>]*>\s*Request #0142 by\s*'
        r'<a href="/user_admin.view">Oma_Gerda</a>\s*</div>',
        out,
    )
    assert meta


def test_update_request_form_header_tags_an_accepted_request(
    update_request_form_env, minimal_app
):
    out = _render_update_request_form(
        update_request_form_env,
        minimal_app,
        party_capacity=240,
        status='accepted',
    )

    header = re.search(r'<h1 class="title"[^>]*>(.*?)</h1>', out, re.DOTALL)
    assert header
    assert '<span class="tag color-info">Accepted</span>' in header.group(1)
    assert 'color-warning' not in header.group(1)


def test_update_request_form_header_does_not_link_a_missing_proposer(
    update_request_form_env, minimal_app
):
    out = _render_update_request_form(
        update_request_form_env,
        minimal_app,
        party_capacity=240,
        proposer_known=False,
        proposer_name='u1',
    )

    assert 'user_admin.view' not in out
    assert re.search(r'Request #0142 by\s*u1\s*</div>', out)


def test_update_request_form_notice_names_the_proposer(
    update_request_form_env, minimal_app
):
    """The notice says "in the request", not "in her request"."""
    out = _render_update_request_form(
        update_request_form_env, minimal_app, party_capacity=240
    )

    assert (
        '<div class="notification color-info block">'
        'Changes are visible to Oma_Gerda right away in the request. '
        'They are recorded in the history as an admin change.</div>'
    ) in out


def test_update_request_form_has_four_fieldsets_in_order(
    update_request_form_env, minimal_app
):
    out = _render_update_request_form(
        update_request_form_env, minimal_app, party_capacity=240
    )

    assert out.count('<fieldset class="fs">') == 4
    assert re.findall(r'<legend>(.*?)</legend>', out) == [
        'General',
        'Format',
        'Time frame',
        'Optional',
    ]


def test_update_request_form_field_rows(update_request_form_env, minimal_app):
    """Three plain `.frow`s and one `.frow.keep` (team size | limit)."""
    out = _render_update_request_form(
        update_request_form_env, minimal_app, party_capacity=240
    )

    assert out.count('class="frow"') == 3
    assert out.count('class="frow keep"') == 1

    keep_row = out[out.index('class="frow keep"') :]
    keep_row = keep_row[: keep_row.index('</fieldset>')]
    assert 'data-team' in keep_row
    assert 'data-limit-inp' in keep_row

    general = out[out.index('<legend>General') :]
    general = general[: general.index('</fieldset>')]
    assert 'name="name"' in general
    assert 'name="game"' in general
    assert 'name="description"' in general
    assert general.index('class="frow"') < general.index('name="description"')


def test_update_request_form_captions(update_request_form_env, minimal_app):
    out = _render_update_request_form(
        update_request_form_env, minimal_app, party_capacity=240
    )

    assert (
        '<div class="form-caption">'
        'Only modes that fit the game format can be selected.</div>'
    ) in out
    assert '<div class="form-caption">1 = solo</div>' in out
    assert '<div class="form-caption">Never publicly visible.</div>' in out


def test_update_request_form_optional_labels_are_hand_rendered(
    update_request_form_env, minimal_app
):
    out = _render_update_request_form(
        update_request_form_env, minimal_app, party_capacity=240
    )

    labels = re.findall(
        r'<label class="form-label" for="(\w+)">([^<]*) '
        r'<span class="dimmed">\(optional\)</span></label>',
        out,
    )
    assert labels == [
        ('special_rules', 'Special rules'),
        ('notes', 'Notes for the orga'),
        ('desired_template', 'Desired template'),
    ]
    assert out.count('(optional)') == 3


def test_update_request_form_textarea_heights(
    update_request_form_env, minimal_app
):
    out = _render_update_request_form(
        update_request_form_env, minimal_app, party_capacity=240
    )

    heights = {
        name: re.search(
            rf'<textarea[^>]*name="{name}"[^>]*style="height:(\w+)"', out
        )
        or re.search(
            rf'<textarea[^>]*style="height:(\w+)"[^>]*name="{name}"', out
        )
        for name in ('description', 'special_rules', 'notes')
    }
    assert {k: m.group(1) for k, m in heights.items() if m} == {
        'description': '5rem',
        'special_rules': '4rem',
        'notes': '3rem',
    }


def test_update_request_form_buttons_are_left_aligned_and_hand_written(
    update_request_form_env, minimal_app
):
    out = _render_update_request_form(
        update_request_form_env, minimal_app, party_capacity=240
    )

    assert 'is-hcentered' not in out
    assert re.search(
        r'<div class="button-row">\s*'
        r'<button type="submit" class="button color-primary">Save</button>\s*'
        r'<a class="button" href="/view_request">Cancel</a>\s*</div>',
        out,
    )
    assert 'form_buttons' not in _UPDATE_REQUEST_FORM_TEMPLATE.read_text()


def test_update_request_form_css_lets_inputs_inherit_the_font():
    """The draft's `font: inherit` gives inputs the 21px line height."""
    css = _MATCH_ADMIN_CSS.read_text()

    rule = re.search(
        r'\.lt-request-page \.lt-edit \.form-control:not\(textarea\) \{'
        r'(.*?)\}',
        css,
        re.DOTALL,
    )
    assert rule
    assert 'font: inherit;' in rule.group(1)


def test_update_request_form_loads_the_admin_stylesheet_and_keeps_the_tabs():
    src = _UPDATE_REQUEST_FORM_TEMPLATE.read_text()

    head = _extract_block(src, 'head')
    assert 'style/lan_tournament_match_admin.css' in head
    assert '{% block before_body %}' not in src


def test_update_request_form_renders_js_hook_contract_with_capacity(
    update_request_form_env, minimal_app
):
    """Contract attributes render per the table (capacity known)."""
    out = _render_update_request_form(
        update_request_form_env, minimal_app, party_capacity=240
    )

    assert 'data-capacity="240"' in out
    assert f'data-max-limit="{MAX_PARTICIPANT_LIMIT}"' in out
    assert 'data-team' in out
    assert 'data-limit-inp' in out
    assert 'data-label-players="Participant limit"' in out
    assert 'data-label-teams="Team limit"' in out


def test_update_request_form_limit_input_carries_the_caption_templates(
    update_request_form_env, minimal_app
):
    """The three site caption msgids, `{capacity}` filled and the rest literal."""
    out = _render_update_request_form(
        update_request_form_env, minimal_app, party_capacity=240
    )

    limit_input = re.search(r'<input[^>]*data-limit-inp[^>]*>', out)
    assert limit_input
    tag = limit_input.group(0)
    assert 'data-caption-template="2 to {max} · 240 seats at the party"' in tag
    assert (
        'data-caption-template-teams="2 to {max} teams · 240 seats ÷ {size}"'
        in tag
    )
    assert (
        'data-caption-template-over="Team size too large for 240 seats"' in tag
    )


@pytest.mark.parametrize(
    ('team_size', 'expected'),
    [
        (1, '2 to 240 · 240 seats at the party'),
        (3, '2 to 80 teams · 240 seats ÷ 3'),
        (200, 'Team size too large for 240 seats'),
    ],
)
def test_update_request_form_initial_limit_caption(
    update_request_form_env, minimal_app, team_size, expected
):
    """The caption element starts as the script would write it on load."""
    out = _render_update_request_form(
        update_request_form_env,
        minimal_app,
        party_capacity=240,
        team_size=team_size,
    )

    assert (
        f'<div class="form-caption" data-limit-caption>{expected}</div>' in out
    )


def test_update_request_form_renders_js_hook_contract_without_capacity(
    update_request_form_env, minimal_app
):
    """AC9: contract attributes are absent exactly as the table says
    (capacity unknown) -- team-size/participant-limit hooks still render."""
    out = _render_update_request_form(
        update_request_form_env, minimal_app, party_capacity=None
    )

    assert 'data-capacity=' not in out
    assert f'data-max-limit="{MAX_PARTICIPANT_LIMIT}"' in out
    assert 'data-team' in out
    assert 'data-limit-inp' in out
    assert 'data-label-players="Participant limit"' in out
    assert 'data-label-teams="Team limit"' in out
    assert 'data-caption-template' not in out
    assert 'data-limit-caption' not in out


def _markup_gettext(overrides: dict[str, str]):
    """A gettext stub returning `Markup` for the given msgids (every
    other msgid passes through unchanged as a plain `str`), the way
    flask_babel's real newstyle gettext does under autoescape. Mirrors
    the identically-named helper in
    test_tournament_request_render_bote.py."""

    def _(s, **kw):
        text = overrides.get(s, s)
        rendered = (text % kw) if kw else text
        return Markup(rendered)  # noqa: S704

    return _


def test_update_request_form_data_labels_escape_markup_gettext(
    update_request_form_env, minimal_app
):
    """The `data-*` attribute values are force-escaped."""
    original_gettext = update_request_form_env.globals['_']
    update_request_form_env.globals['_'] = _markup_gettext(
        {
            'Participant limit': 'Cap "quoted"',
            'Team limit': 'Team "quoted"',
            '2 to {max} · {capacity} seats at the party': (
                '2 to {max} "quoted" · {capacity} seats'
            ),
            '2 to {max} teams · {capacity} seats ÷ {size}': (
                '2 to {max} "quoted" teams · {capacity} seats ÷ {size}'
            ),
            'Team size too large for {capacity} seats': (
                'Too "quoted" for {capacity} seats'
            ),
        }
    )
    try:
        out = _render_update_request_form(
            update_request_form_env, minimal_app, party_capacity=240
        )
    finally:
        update_request_form_env.globals['_'] = original_gettext

    assert 'data-label-players="Cap &#34;quoted&#34;"' in out
    assert 'data-label-teams="Team &#34;quoted&#34;"' in out
    assert (
        'data-caption-template="2 to {max} &#34;quoted&#34; · 240 seats"' in out
    )
    assert (
        'data-caption-template-teams="2 to {max} &#34;quoted&#34; teams '
        '· 240 seats ÷ {size}"' in out
    )
    assert (
        'data-caption-template-over="Too &#34;quoted&#34; for 240 seats"' in out
    )
    # A raw, unescaped quote from the Markup-wrapped msgstr would have
    # broken out of the attribute -- must never appear.
    assert 'Cap "quoted"' not in out
    assert 'Team "quoted"' not in out


def _elimination_mode_option_reasons_by_value(out: str) -> dict[str, dict]:
    """Map each elimination-mode `<option>`'s `value` to its parsed
    `data-reasons` JSON.

    WTForms' `html_params` HTML-escapes the attribute value (quotes
    become `&#34;`); `html.unescape` undoes that the same way a
    browser decodes an attribute before handing it to `dataset`/
    `getAttribute`. AC1 requires asserting on the parsed JSON, not a
    substring.
    """
    select_match = re.search(
        r'<select[^>]*name="elimination_mode".*?</select>', out, re.DOTALL
    )
    assert select_match, 'no elimination_mode <select> in output'

    result = {}
    for tag in re.findall(r'<option\b[^>]*>', select_match.group(0)):
        value_match = re.search(r'value="([^"]*)"', tag)
        reasons_match = re.search(r'data-reasons="([^"]*)"', tag)
        assert value_match, f'no value attribute: {tag!r}'
        assert reasons_match, f'no data-reasons attribute: {tag!r}'
        result[value_match.group(1)] = json.loads(
            html.unescape(reasons_match.group(1))
        )
    return result


def test_update_request_form_option_reasons_json(
    update_request_form_env, minimal_app
):
    """AC1 (workspace-dim0.15): every elimination-mode `<option>`
    carries a `data-reasons` JSON mapping every `GameFormat` value to
    that mode's reason (`None` when valid), computed through the real
    `set_format_choices()` against the real domain service -- this is
    the FIX-3 bug: without it, a fresh Highscore pick left every mode
    disabled server-render-only, with no way for the browser to
    re-evaluate after the switch.
    """
    out = _render_update_request_form(
        update_request_form_env, minimal_app, party_capacity=240
    )

    reasons_by_value = _elimination_mode_option_reasons_by_value(out)

    assert reasons_by_value['SINGLE_ELIMINATION'] == {
        'ONE_V_ONE': None,
        'FREE_FOR_ALL': None,
        'HIGHSCORE': 'not with Highscore',
    }
    assert reasons_by_value['DOUBLE_ELIMINATION'] == {
        'ONE_V_ONE': None,
        'FREE_FOR_ALL': None,
        'HIGHSCORE': 'not with Highscore',
    }
    assert reasons_by_value['ROUND_ROBIN'] == {
        'ONE_V_ONE': None,
        'FREE_FOR_ALL': 'only 1v1',
        'HIGHSCORE': 'only 1v1',
    }
    assert reasons_by_value['NONE'] == {
        'ONE_V_ONE': 'only Highscore',
        'FREE_FOR_ALL': 'only Highscore',
        'HIGHSCORE': None,
    }

    # AC1's specific pin: Highscore -> null for NONE, 1v1 -> null for
    # the modes valid under 1v1.
    assert reasons_by_value['NONE']['HIGHSCORE'] is None
    assert reasons_by_value['SINGLE_ELIMINATION']['ONE_V_ONE'] is None
    assert reasons_by_value['ROUND_ROBIN']['ONE_V_ONE'] is None

    # `data-label-base` carries the plain label, no reason suffix, so
    # the JS can rewrite a disabled `<option>`'s text back to plain
    # once its format becomes valid again.
    assert 'data-label-base="Single knockout"' in out
    assert 'data-label-base="No knockout"' in out


# --------------------------------------------------------------------- #
# view_request.html -- deleted-tournament state (workspace-dim0.18)
# --------------------------------------------------------------------- #
#
# Unlike `_make_request` above (a `SimpleNamespace` stub), these build a
# real `TournamentRequest` dataclass so `tournament_deleted` is the
# actual computed property (`status is tournament_created and
# created_tournament_id is None`), not a hand-set stub attribute.


def _make_real_request(
    *,
    status=TournamentRequestStatus.tournament_created,
    created_tournament_id=None,
    number=9,
    name='Real Cup',
    decided_at=datetime(2026, 1, 2, 9, 0),
    decided_by_id=None,
    rejection_reason=None,
    updated_at=datetime(2026, 1, 3, 9, 0),
):
    now = datetime(2026, 1, 1, 10, 0)
    if decided_by_id is None:
        decided_by_id = UserID(generate_uuid())
    return TournamentRequest(
        id=TournamentRequestID(generate_uuid()),
        party_id=PartyID('lan-2026'),
        number=number,
        proposer_id=UserID(generate_uuid()),
        created_at=now,
        status=status,
        name=name,
        game='Some Game',
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        team_size=1,
        participant_limit=16,
        preferred_start_time=now,
        preferred_end_time=now,
        description='A friendly bracket.',
        created_tournament_id=created_tournament_id,
        decided_at=decided_at,
        decided_by_id=decided_by_id,
        rejection_reason=rejection_reason,
        updated_at=updated_at,
    )


def _decision_url_for(endpoint, **kwargs):
    path = '/' + endpoint.lstrip('.')
    if not kwargs:
        return path
    qs = '&'.join(f'{k}={v}' for k, v in sorted(kwargs.items()))
    return f'{path}?{qs}'


@pytest.fixture(scope='module')
def decision_env():
    src = _VIEW_REQUEST_TEMPLATE.read_text()
    start = src.index("<h2>{{ _('Decision') }}</h2>")
    end = src.rindex('{%- endblock %}')
    fragment = src[start:end]

    template = (
        "{% from 'macros/misc.html' import render_notification_block %}\n"
        "{% from 'macros/forms.html' import form_field_errors %}\n" + fragment
    )

    env = _make_env(
        {
            'decision': template,
            'macros/misc.html': _MACROS_MISC,
            'macros/forms.html': _MACROS_FORMS,
        }
    )
    env.globals['url_for'] = _decision_url_for
    env.filters['dateformat'] = lambda dt, *a, **k: dt.strftime('%Y-%m-%d')
    env.filters['timeformat'] = lambda dt, *a, **k: dt.strftime('%H:%M')
    return env


def _render_decision(
    env,
    *,
    tournament_request,
    party,
    can_create,
    can_decide=True,
    proposer_name='Proposer',
    reject_form=None,
    created_tournament=None,
    proposer_is_orga=False,
    users_by_id=None,
    field_labels=None,
    gap=None,
):
    tmpl = env.get_template('decision')
    user = SimpleNamespace(
        has_permission=lambda perm: (
            (can_create and perm == 'lan_tournament.create')
            or (can_decide and perm == 'lan_tournament.request_decide')
        )
    )
    if reject_form is None:
        # `errors=[]` mirrors a fresh, unsubmitted WTForms field.
        reject_form = SimpleNamespace(reason=_FakeReasonField('', errors=[]))
    if field_labels is None:
        field_labels = {
            'point_table': 'Points by placement',
            'group_size_max': 'Max. group size',
        }
    if gap is None:
        gap = _make_gap(blocking=[])
    return tmpl.render(
        tournament_request=tournament_request,
        party=party,
        proposer_name=proposer_name,
        reject_form=reject_form,
        g=SimpleNamespace(user=user),
        created_tournament=created_tournament,
        proposer_is_orga=proposer_is_orga,
        users_by_id=users_by_id or {},
        field_labels=field_labels,
        gap=gap,
    )


def test_view_request_decision_shows_deleted_note_and_recreate_button(
    decision_env,
):
    """AC1/AC2: the warning notification and the exact re-create href."""
    tournament_request = _make_real_request(
        status=TournamentRequestStatus.tournament_created,
        created_tournament_id=None,
    )
    party = SimpleNamespace(id='p1')

    out = _render_decision(
        decision_env,
        tournament_request=tournament_request,
        party=party,
        can_create=True,
    )

    assert 'notification color-warning' in out
    assert (
        'The tournament created from this request has been deleted.' in out
    )
    # Jinja autoescapes the `&` separator in the rendered attribute.
    expected_href = (
        f'href="/create_form?from_request={tournament_request.id}'
        f'&amp;party_id={party.id}"'
    )
    assert expected_href in out
    assert 'Create tournament again' in out


def test_view_request_decision_hides_recreate_button_without_permission(
    decision_env,
):
    """AC2: the button is gated exactly like the sibling `Create
    tournament` button -- `lan_tournament.create` -- the notification
    itself is not permission-gated (viewing the page already required
    `request_view`)."""
    tournament_request = _make_real_request(
        status=TournamentRequestStatus.tournament_created,
        created_tournament_id=None,
    )
    party = SimpleNamespace(id='p1')

    out = _render_decision(
        decision_env,
        tournament_request=tournament_request,
        party=party,
        can_create=False,
    )

    assert (
        'The tournament created from this request has been deleted.' in out
    )
    assert 'Create tournament again' not in out
    assert 'create_form' not in out


def test_view_request_decision_hides_recreate_button_without_request_decide(
    decision_env,
):
    """J1: `lan_tournament.create` alone must no longer show the
    re-create button either -- submitting it would only be refused
    (and clear the hidden link) by `create`'s own from-request gate,
    which now also requires `request_decide`."""
    tournament_request = _make_real_request(
        status=TournamentRequestStatus.tournament_created,
        created_tournament_id=None,
    )
    party = SimpleNamespace(id='p1')

    out = _render_decision(
        decision_env,
        tournament_request=tournament_request,
        party=party,
        can_create=True,
        can_decide=False,
    )

    assert (
        'The tournament created from this request has been deleted.' in out
    )
    assert 'Create tournament again' not in out
    assert 'create_form' not in out


def test_view_request_decision_shows_create_button_for_accepted_when_permitted(
    decision_env,
):
    """AC7: the sibling `accepted`-status `Create tournament` button
    (previously ungated in this file) is now gated the same way."""
    tournament_request = _make_real_request(
        status=TournamentRequestStatus.accepted,
    )
    party = SimpleNamespace(id='p1')

    out = _render_decision(
        decision_env,
        tournament_request=tournament_request,
        party=party,
        can_create=True,
    )

    assert '>Create tournament<' in out
    assert 'create_form' in out


def test_view_request_decision_hides_create_button_for_accepted_without_permission(
    decision_env,
):
    """AC7: without `lan_tournament.create`, the accepted-status
    button is absent -- the unrelated disabled "Appoint as orga"
    button still renders, since only the create button changed."""
    tournament_request = _make_real_request(
        status=TournamentRequestStatus.accepted,
    )
    party = SimpleNamespace(id='p1')

    out = _render_decision(
        decision_env,
        tournament_request=tournament_request,
        party=party,
        can_create=False,
    )

    assert '>Create tournament<' not in out
    assert 'create_form' not in out
    assert 'Appoint Proposer as orga' in out


def test_view_request_decision_hides_create_button_for_accepted_without_request_decide(
    decision_env,
):
    """J1: the core regression case -- an admin with `create` but not
    `request_decide` must not even see the button, since submitting it
    is now refused by `create`'s from-request gate."""
    tournament_request = _make_real_request(
        status=TournamentRequestStatus.accepted,
    )
    party = SimpleNamespace(id='p1')

    out = _render_decision(
        decision_env,
        tournament_request=tournament_request,
        party=party,
        can_create=True,
        can_decide=False,
    )

    assert '>Create tournament<' not in out
    assert 'create_form' not in out
    assert 'Appoint Proposer as orga' in out


def test_view_request_decision_shows_the_ffa_caption_while_fields_block(
    decision_env,
):
    """The caption under "Create tournament" shows while blocked."""
    tournament_request = _make_real_request(
        status=TournamentRequestStatus.accepted,
    )

    out = _render_decision(
        decision_env,
        tournament_request=tournament_request,
        party=SimpleNamespace(id='p1'),
        can_create=True,
        gap=_make_gap(blocking=['point_table', 'group_size_max']),
    )

    assert (
        '<div class="form-caption" style="color: #c00;">'
        'Still required in the form: points by placement, max. group size'
        '</div>'
    ) in out


def test_view_request_decision_hides_the_ffa_caption_without_blockers(
    decision_env,
):
    tournament_request = _make_real_request(
        status=TournamentRequestStatus.accepted,
    )

    out = _render_decision(
        decision_env,
        tournament_request=tournament_request,
        party=SimpleNamespace(id='p1'),
        can_create=True,
        gap=_make_gap(blocking=[]),
    )

    assert 'Still required in the form' not in out


def test_view_request_decision_offers_accept_for_an_open_request(
    decision_env,
):
    tournament_request = _make_real_request(
        status=TournamentRequestStatus.submitted,
    )

    out = _render_decision(
        decision_env,
        tournament_request=tournament_request,
        party=SimpleNamespace(id='p1'),
        can_create=True,
    )

    assert f'action="/accept_request?request_id={tournament_request.id}"' in out
    assert '<button type="submit" class="button color-success lt-wide">' in out
    assert (
        '<div class="form-caption">The request is frozen. You create the '
        'tournament next.</div>'
    ) in out
    assert 'notification' not in out


def test_view_request_decision_accepted_note_is_a_plain_info_notification(
    decision_env,
):
    """The note is hand-written, without a notification icon block."""
    decider_id = UserID(generate_uuid())
    tournament_request = _make_real_request(
        status=TournamentRequestStatus.accepted,
        decided_at=datetime(2026, 1, 2, 9, 0),
        decided_by_id=decider_id,
    )

    out = _render_decision(
        decision_env,
        tournament_request=tournament_request,
        party=SimpleNamespace(id='p1'),
        can_create=True,
        users_by_id={decider_id: SimpleNamespace(screen_name='Test_User_1')},
    )

    note = re.search(
        r'<div class="notification color-info[^"]*"[^>]*>(.*?)</div>',
        out,
        re.DOTALL,
    )
    assert note is not None
    assert (
        '<div class="notification color-info block" style="font-size: 13px;">'
    ) in out
    assert 'Accepted by Test_User_1 on 2026-01-02, 09:00' in note.group(1)
    assert 'block-with-icon' not in out


def test_view_request_decision_accepted_lists_actions_in_the_draft_order(
    decision_env,
):
    tournament_request = _make_real_request(
        status=TournamentRequestStatus.accepted,
    )

    out = _render_decision(
        decision_env,
        tournament_request=tournament_request,
        party=SimpleNamespace(id='p1'),
        can_create=True,
        gap=_make_gap(blocking=['point_table', 'group_size_max']),
    )

    positions = [
        out.index('Accepted by'),
        out.index('>Create tournament<'),
        out.index('Still required in the form'),
        out.index('<hr class="sep-line">'),
        out.index('Appoint Proposer as orga'),
        out.index('Available once the tournament is created.'),
        out.rindex('<hr class="sep-line">'),
        out.index('<details class="rej"'),
    ]
    assert positions == sorted(positions)


def test_view_request_decision_accepted_appoint_button_is_disabled_with_caption(
    decision_env,
):
    tournament_request = _make_real_request(
        status=TournamentRequestStatus.accepted,
    )

    out = _render_decision(
        decision_env,
        tournament_request=tournament_request,
        party=SimpleNamespace(id='p1'),
        can_create=True,
    )

    assert (
        '<button type="button" class="button lt-wide" disabled>'
        'Appoint Proposer as orga</button>'
    ) in out
    assert (
        '<div class="form-caption">Available once the tournament is '
        'created.</div>'
    ) in out
    assert 'Happens automatically' not in out


def test_decision_box_links_created_tournament(decision_env):
    """AC2: a live-linked `tournament_created` request links the
    Decision box to the created tournament -- it must not render
    empty, nor the deleted note/re-create button of the sibling
    (deleted-link) case."""
    tournament_request = _make_real_request(
        status=TournamentRequestStatus.tournament_created,
        created_tournament_id=TournamentID(generate_uuid()),
    )
    party = SimpleNamespace(id='p1')
    created_tournament = SimpleNamespace(
        id=tournament_request.created_tournament_id, name='Spring Cup'
    )

    out = _render_decision(
        decision_env,
        tournament_request=tournament_request,
        party=party,
        can_create=True,
        created_tournament=created_tournament,
        proposer_is_orga=True,
    )

    assert 'Tournament created' in out
    assert 'Spring Cup' in out
    assert f'href="/view?tournament_id={created_tournament.id}"' in out
    assert 'is an orga of this tournament' in out
    assert 'has been deleted' not in out
    assert 'Create tournament again' not in out


def test_decision_box_shows_rejection_reason_and_decider(decision_env):
    """AC2: a rejected request names the decider, the decision date
    and shows the rejection reason -- the box previously rendered
    completely empty for this state."""
    decider_id = UserID(generate_uuid())
    tournament_request = _make_real_request(
        status=TournamentRequestStatus.rejected,
        decided_at=datetime(2026, 2, 1, 9, 30),
        decided_by_id=decider_id,
        rejection_reason='Venue unavailable that weekend.',
    )
    party = SimpleNamespace(id='p1')

    out = _render_decision(
        decision_env,
        tournament_request=tournament_request,
        party=party,
        can_create=True,
        users_by_id={decider_id: SimpleNamespace(screen_name='Test_User_1')},
    )

    assert 'Test_User_1' in out
    assert '2026-02-01' in out
    assert '09:30' in out
    assert 'Venue unavailable that weekend.' in out


def test_decision_box_shows_withdrawn_note(decision_env):
    """AC2: a withdrawn request shows the withdrawal date -- the box
    previously rendered completely empty for this state too."""
    tournament_request = _make_real_request(
        status=TournamentRequestStatus.withdrawn,
        updated_at=datetime(2026, 3, 1, 14, 15),
    )
    party = SimpleNamespace(id='p1')

    out = _render_decision(
        decision_env,
        tournament_request=tournament_request,
        party=party,
        can_create=True,
    )

    assert 'Withdrawn' in out
    assert '2026-03-01' in out
    assert '14:15' in out


# --------------------------------------------------------------------- #
# view_request.html -- reject <details> auto-open + preserved text
# --------------------------------------------------------------------- #


class _FakeReasonField:
    """Mimic just enough of a WTForms field for the reject `<details>`."""

    id = 'reason'

    def __init__(self, data, errors=()):
        self.data = data
        self.errors = list(errors)

    def __call__(self, **kwargs):
        attrs = Markup('').join(
            Markup(' {}="{}"').format(key, value)
            for key, value in kwargs.items()
        )
        return Markup('<textarea name="reason"{}>{}</textarea>').format(
            attrs, self.data
        )


@pytest.fixture(scope='module')
def reject_details_env():
    src = _VIEW_REQUEST_TEMPLATE.read_text()
    start = src.index(
        "{%- if tournament_request.status.value in "
        "('submitted', 'accepted') %}"
    )
    end = src.index('{%- endif %}', src.index('</details>', start))
    fragment = src[start : end + len('{%- endif %}')]

    template = (
        "{% from 'macros/forms.html' import form_field_errors %}\n" + fragment
    )

    env = _make_env(
        {'reject_details': template, 'macros/forms.html': _MACROS_FORMS}
    )
    return env


def _render_reject_details(
    env, *, tournament_request, reject_form, proposer_name='Oma_Gerda'
):
    tmpl = env.get_template('reject_details')
    return tmpl.render(
        tournament_request=tournament_request,
        reject_form=reject_form,
        proposer_name=proposer_name,
    )


def test_view_request_reject_details_preserves_text_and_opens_on_error(
    reject_details_env,
):
    """AC (n): a too-long reject reason is re-rendered, not lost -- the
    `<details>` auto-opens so the admin sees it and the erroneous
    field without having to click it open again."""
    tournament_request = _make_real_request(
        status=TournamentRequestStatus.submitted
    )
    long_reason = 'x' * 2001
    reject_form = SimpleNamespace(
        reason=_FakeReasonField(
            long_reason,
            errors=['Field cannot be longer than 2000 characters.'],
        )
    )

    out = _render_reject_details(
        reject_details_env,
        tournament_request=tournament_request,
        reject_form=reject_form,
    )

    assert '<details class="rej" open>' in out
    assert long_reason in out


def test_view_request_reject_details_closed_without_errors(
    reject_details_env,
):
    """A freshly opened (unsubmitted) reject form stays collapsed."""
    tournament_request = _make_real_request(
        status=TournamentRequestStatus.submitted
    )
    reject_form = SimpleNamespace(reason=_FakeReasonField('', errors=[]))

    out = _render_reject_details(
        reject_details_env,
        tournament_request=tournament_request,
        reject_form=reject_form,
    )

    assert '<details class="rej">' in out
    assert '<details class="rej" open>' not in out
    assert 'invalid' not in out
    assert 'form-errors' not in out


@pytest.mark.parametrize(
    ('status', 'label'),
    [
        (TournamentRequestStatus.submitted, 'Reject…'),
        (TournamentRequestStatus.accepted, 'Reject after all …'),
    ],
)
def test_view_request_reject_summary_names_the_action_per_status(
    reject_details_env, status, label
):
    out = _render_reject_details(
        reject_details_env,
        tournament_request=_make_real_request(status=status),
        reject_form=SimpleNamespace(reason=_FakeReasonField('')),
    )

    assert f'<summary class="button lt-wide">{label}</summary>' in out


def test_view_request_reject_body_matches_the_draft(reject_details_env):
    out = _render_reject_details(
        reject_details_env,
        tournament_request=_make_real_request(
            status=TournamentRequestStatus.submitted
        ),
        reject_form=SimpleNamespace(reason=_FakeReasonField('')),
        proposer_name='Oma_Gerda',
    )

    assert '<form action=".reject_request" method="post">' in out
    assert '<div class="rb">' in out
    assert (
        '<label class="form-label" for="reason">Reason '
        '<span class="dimmed">(required, visible to Oma_Gerda)</span>'
        '</label>'
    ) in out
    assert (
        '<textarea name="reason" class="form-control" '
        'style="height: 7rem;"></textarea>'
    ) in out
    assert '<div class="form-caption">Rejecting is final.</div>' in out
    assert re.search(
        r'<div class="button-row" style="margin-top: 12px;">\s*'
        r'<button type="submit" class="button color-danger">'
        r'Reject request</button>',
        out,
    )
    assert out.index('</summary>') < out.index('<div class="rb">')


def test_view_request_reject_body_uses_no_shared_form_macros():
    """The label, caption and button row are written out by hand."""
    src = _VIEW_REQUEST_TEMPLATE.read_text()

    assert 'form_buttons' not in src
    assert 'form_field(' not in src


def test_view_request_reject_details_highlights_and_lists_field_errors(
    reject_details_env,
):
    message = 'Field cannot be longer than 2000 characters.'
    out = _render_reject_details(
        reject_details_env,
        tournament_request=_make_real_request(
            status=TournamentRequestStatus.submitted
        ),
        reject_form=SimpleNamespace(
            reason=_FakeReasonField('x' * 2001, errors=[message])
        ),
    )

    assert '<div class="form-control-block invalid">' in out
    assert '<ol class="form-errors">' in out
    assert f'<span>{message}</span>' in out
    assert out.index('form-errors') < out.index('Rejecting is final.')


@pytest.mark.parametrize(
    'status',
    [
        TournamentRequestStatus.tournament_created,
        TournamentRequestStatus.rejected,
        TournamentRequestStatus.withdrawn,
    ],
)
def test_view_request_reject_details_absent_for_decided_requests(
    reject_details_env, status
):
    out = _render_reject_details(
        reject_details_env,
        tournament_request=_make_real_request(status=status),
        reject_form=SimpleNamespace(reason=_FakeReasonField('')),
    )

    assert '<details' not in out


def test_view_request_has_no_history_box():
    """The detail shows the decision, not the audit trail."""
    src = _VIEW_REQUEST_TEMPLATE.read_text()

    assert '{# History #}' in src
    assert "_('History')" not in src
    assert 'No history yet.' not in src
    assert 'for entry in history' not in src
    boundary = src[
        src.index('{# History #}') : src.index('<div class="lt-decision box">')
    ]
    assert '<table' not in boundary
    assert '<h2' not in boundary


def test_view_request_slice_markers_keep_their_order():
    """The render fixtures cut the template at these literal markers."""
    src = _VIEW_REQUEST_TEMPLATE.read_text()

    order = [
        src.index('{# Six data tiles'),
        src.index('{# Gap preview: what create_tournament still needs'),
        src.index('{# History #}'),
        src.index("<h2>{{ _('Decision') }}</h2>"),
    ]
    assert order == sorted(order)


# --------------------------------------------------------------------- #
# requests_for_party.html -- done-list tag for a deleted tournament
# --------------------------------------------------------------------- #


def test_requests_for_party_done_list_tags_deleted_and_live_correctly(
    queue_env,
):
    deleted_request = _make_real_request(
        status=TournamentRequestStatus.tournament_created,
        created_tournament_id=None,
        number=3,
        name='Deleted Cup',
    )
    live_request = _make_real_request(
        status=TournamentRequestStatus.tournament_created,
        created_tournament_id=TournamentID(generate_uuid()),
        number=4,
        name='Live Cup',
    )

    out = _render_queue(
        queue_env,
        pending_requests=[],
        done_requests=[deleted_request, live_request],
        gaps_by_request_id={},
    )

    # `.count('Tournament created')` would also match the status-filter
    # bar's own tag, unrelated to the done-list row -- assert the exact
    # rendered tag markup instead. It renders once in the desktop table
    # and once more in the mirrored mobile itemlist.
    assert out.count('Tournament deleted') == 2
    assert 'class="tag color-disabled">Tournament deleted' in out
    assert 'class="tag color-success">Tournament created' in out
