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

from tests.helpers import generate_uuid


_VIEW_REQUEST_TEMPLATE = pathlib.Path(
    'byceps/services/lan_tournament/blueprints/admin/templates'
    '/admin/lan_tournament/view_request.html'
)
_QUEUE_TEMPLATE = pathlib.Path(
    'byceps/services/lan_tournament/blueprints/admin/templates'
    '/admin/lan_tournament/requests_for_party.html'
)
_CREATE_FORM_TEMPLATE = pathlib.Path(
    'byceps/services/lan_tournament/blueprints/admin/templates'
    '/admin/lan_tournament/create_form.html'
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

{% macro render_distribution_bar(values_and_classes, total) -%}
<div class="progress">
{%- for value, cls in values_and_classes %}{% if value %}<div class="progress-bar {{ cls }}"></div>{% endif %}{% endfor -%}
</div>
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


def _make_gap(*, blocking=()):
    return SimpleNamespace(
        supplied=[
            'name',
            'game',
            'game_format',
            'elimination_mode',
            'team_size',
            'participant_limit',
            'preferred_start_time',
            'preferred_end_time',
            'description',
            'ruleset',
            'party',
            'origin',
        ],
        admin_fills=[
            'contestant_type',
            'score_ordering',
            'min_players',
            'min_teams',
            'image_url',
            'advancement_count',
            'points_carry_to_losers',
        ],
        blocking=list(blocking),
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

    template = (
        "{% from 'macros/misc.html' import "
        'render_distribution_bar, render_notification_block, render_tag '
        '%}\n' + fragment
    )

    return _make_env(
        {'gap_preview': template, 'macros/misc.html': _MACROS_MISC}
    )


def _render_gap_preview(env, *, blocking):
    tmpl = env.get_template('gap_preview')
    return tmpl.render(gap=_make_gap(blocking=blocking))


def test_view_request_renders_gap_preview_with_blockers(gap_preview_env):
    out = _render_gap_preview(
        gap_preview_env, blocking=['point_table', 'group_size_max']
    )

    # Progress bar, split into the three categories.
    assert 'progress' in out
    assert 'progress-bar color-success' in out
    assert 'progress-bar color-disabled' in out
    assert 'progress-bar color-danger' in out

    # Legend.
    assert 'Supplied by the request' in out
    assert 'Filled in by the admin' in out
    assert 'Blocks tournament creation' in out

    # The FFA blocker notification names both blocking fields.
    assert 'notification' in out
    assert 'color-danger' in out
    assert 'Points by placement' in out
    assert 'Max. group size' in out

    # "Still missing" lists the blockers as gated rows.
    assert 'Still missing' in out
    assert 'class="gate"' in out

    # "Prefilled from the request" lists the supplied fields.
    assert 'Prefilled from the request' in out


def test_view_request_gap_preview_has_no_blocker_notice_without_blockers(
    gap_preview_env,
):
    out = _render_gap_preview(gap_preview_env, blocking=[])

    assert 'notification' not in out
    assert 'Points by placement' not in out
    assert 'class="gate"' not in out
    assert 'Still missing' in out


# --------------------------------------------------------------------- #
# view_request.html -- elimination mode tile shows a translated label
# (J4)
# --------------------------------------------------------------------- #


@pytest.fixture(scope='module')
def elimination_mode_tile_env():
    src = _VIEW_REQUEST_TEMPLATE.read_text()
    marker = (
        '<div class="data-label">{{ _(\'Elimination mode\') }}</div>'
    )
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


# --------------------------------------------------------------------- #
# view_request.html -- edit button (workspace-dim0.9, compromise C6)
# --------------------------------------------------------------------- #


def _make_header_request(*, is_editable=True, status='submitted'):
    return SimpleNamespace(
        id='r1',
        number=142,
        name='Some Tournament',
        status=SimpleNamespace(value=status),
        is_editable=is_editable,
    )


@pytest.fixture(scope='module')
def header_env():
    src = _VIEW_REQUEST_TEMPLATE.read_text()
    start = src.index('<div class="row row--space-between block">')
    end = src.index(
        '<div class="row row--space-between row--wrap block">', start
    )
    fragment = src[start:end]

    template = "{% from 'macros/misc.html' import render_tag %}\n" + fragment

    return _make_env({'header': template, 'macros/misc.html': _MACROS_MISC})


def _render_header(env, *, tournament_request, can_decide):
    tmpl = env.get_template('header')
    user = SimpleNamespace(
        has_permission=lambda perm: (
            can_decide and perm == 'lan_tournament.request_decide'
        )
    )
    return tmpl.render(
        tournament_request=tournament_request, g=SimpleNamespace(user=user)
    )


def test_view_request_shows_edit_button_when_editable_and_permitted(
    header_env,
):
    out = _render_header(
        header_env,
        tournament_request=_make_header_request(is_editable=True),
        can_decide=True,
    )

    assert 'Edit' in out
    assert 'update_request_form' in out


def test_view_request_hides_edit_button_without_request_decide_permission(
    header_env,
):
    out = _render_header(
        header_env,
        tournament_request=_make_header_request(is_editable=True),
        can_decide=False,
    )

    assert '>Edit<' not in out


def test_view_request_hides_edit_button_when_request_not_editable(
    header_env,
):
    out = _render_header(
        header_env,
        tournament_request=_make_header_request(
            is_editable=False, status='accepted'
        ),
        can_decide=True,
    )

    assert '>Edit<' not in out


# --------------------------------------------------------------------- #
# view_request.html -- proposer_name falls back for a deleted proposer
# (G5, bead workspace-c9o4.3)
# --------------------------------------------------------------------- #


@pytest.fixture(scope='module')
def proposer_name_env():
    src = _VIEW_REQUEST_TEMPLATE.read_text()
    start = src.index('{% block body %}') + len('{% block body %}')
    end = src.index('<div class="row row--space-between block">', start)
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
        "{% from 'macros/misc.html' import "
        'render_notification_block, render_tag %}\n' + fragment
    )

    return _make_env(
        {
            'queue': template,
            'macros/misc.html': _MACROS_MISC,
            'macros/admin.html': _MACROS_ADMIN,
        }
    )


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
        status_counts={r.status.value: 1 for r in all_requests},
        status_filter=None,
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
    # is attached to it -- only one such notice should render overall.
    assert out.count('required fields are missing') == 1

    # Done section renders the terminal status.
    assert 'Tournament created' in out


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

    assert out.count('Deleted user') == 2
    assert 'None' not in out


def test_requests_for_party_renders_empty_queue(queue_env):
    out = _render_queue(
        queue_env, pending_requests=[], done_requests=[], gaps_by_request_id={}
    )

    assert 'No tournament requests are awaiting a decision.' in out
    assert 'No requests have been decided yet.' in out


# --------------------------------------------------------------------- #
# requests_for_party.html -- stale-accepted warning (workspace-dim0.16)
# --------------------------------------------------------------------- #


def test_requests_for_party_marks_only_the_stale_accepted_row(queue_env):
    fresh_accepted = _make_request(
        request_id='r1', number=7, proposer_id='u1', status='accepted'
    )
    stale_accepted = _make_request(
        request_id='r2', number=9, proposer_id='u2', status='accepted'
    )

    out = _render_queue(
        queue_env,
        pending_requests=[fresh_accepted, stale_accepted],
        done_requests=[],
        gaps_by_request_id={
            'r1': _make_gap(blocking=[]),
            'r2': _make_gap(blocking=[]),
        },
        stale_request_ids={'r2'},
    )

    # Exact attribute match -- a substring check on "is-accepted" would
    # also match the stale row's "is-accepted is-stale".
    assert out.count('class="is-accepted"') == 1
    assert out.count('class="is-accepted is-stale"') == 1

    # Exactly one "No tournament yet" tag, on the stale row only.
    assert out.count('No tournament yet') == 1

    # The warning notification is present and states the count.
    assert 'notification' in out
    assert 'color-warning' in out
    assert '1 accepted request is still waiting for its tournament.' in out


def test_requests_for_party_has_no_stale_markers_without_stale_rows(
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
        stale_request_ids=set(),
    )

    assert 'class="is-accepted"' in out
    assert 'is-stale' not in out
    assert 'No tournament yet' not in out
    assert 'notification' not in out


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
        "{% from 'macros/forms.html' import form_buttons, form_field %}\n"
        "{% from 'macros/misc.html' import render_notification_block %}\n"
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


def _render_update_request_form(env, app, *, party_capacity):
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
                'team_size': 1,
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
            tournament_request=SimpleNamespace(id='r1', number=142),
            party_capacity=party_capacity,
            page_title='Edit request',
        )


def test_update_request_form_renders(update_request_form_env, minimal_app):
    """AC3/AC7: the admin edit form renders under `StrictUndefined`.

    Covers the `fieldset.fs` groups, the never-hidden disabled
    elimination-mode option (decision D10), the proposer-visibility
    notice, and the JS hook script tag placed after the form.
    """
    out = _render_update_request_form(
        update_request_form_env, minimal_app, party_capacity=240
    )

    # Four `fieldset.fs` groups, in the briefing's order.
    assert out.count('<fieldset class="fs') == 4
    assert 'General' in out
    assert 'Format' in out
    assert 'Time frame' in out
    assert 'Optional' in out

    # NONE is invalid for the selected ONE_V_ONE format -- rendered
    # disabled, never hidden. Match the option by its `value`, not by
    # attribute order: `data-reasons`/`data-label-base` (workspace-
    # dim0.15) now sort ahead of `disabled` in WTForms' `html_params`.
    none_option = re.search(r'<option[^>]*value="NONE"[^>]*>', out)
    assert none_option, 'no <option value="NONE"> in output'
    assert 'disabled' in none_option.group(0)

    # The proposer-visibility notification.
    assert 'class="notification color-info' in out
    assert 'recorded in the request' in out

    # The JS hook script tag renders after the form.
    form_index = out.index('<form')
    script_index = out.index('src="/static/behavior/lan_tournament_request.js"')
    assert script_index > form_index


def test_update_request_form_renders_js_hook_contract_with_capacity(
    update_request_form_env, minimal_app
):
    """AC9: contract attributes render per the table (capacity known)."""
    out = _render_update_request_form(
        update_request_form_env, minimal_app, party_capacity=240
    )

    assert 'data-capacity="240"' in out
    assert f'data-max-limit="{MAX_PARTICIPANT_LIMIT}"' in out
    assert 'data-team' in out
    assert 'data-limit-inp' in out
    assert 'data-label-players="Participant limit"' in out
    assert 'data-label-teams="Team limit"' in out
    assert (
        'data-caption-template="At most {max} with this team size '
        '(party capacity: 240)."' in out
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
    """J5: `_()` returns `Markup` under real flask_babel autoescape,
    and WTForms' `html_params` (the attribute renderer `form_field`'s
    field call ultimately goes through) trusts a `Markup` value as
    already-safe and does not escape it -- without `|forceescape` on
    the three `data-*` kwargs, a msgstr containing a literal `"` would
    break out of the `data-label-players`/`data-label-teams`/
    `data-caption-template` attribute entirely."""
    original_gettext = update_request_form_env.globals['_']
    update_request_form_env.globals['_'] = _markup_gettext(
        {
            'Participant limit': 'Cap "quoted"',
            'Team limit': 'Team "quoted"',
            'At most {max} with this team size '
            '(party capacity: %(capacity)s).': (
                'At most {max} "quoted" (party capacity: %(capacity)s).'
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
        'data-caption-template="At most {max} &#34;quoted&#34; '
        '(party capacity: 240)."' in out
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
        'HIGHSCORE': 'Not available for Highscore',
    }
    assert reasons_by_value['DOUBLE_ELIMINATION'] == {
        'ONE_V_ONE': None,
        'FREE_FOR_ALL': None,
        'HIGHSCORE': 'Not available for Highscore',
    }
    assert reasons_by_value['ROUND_ROBIN'] == {
        'ONE_V_ONE': None,
        'FREE_FOR_ALL': 'Only available for 1v1',
        'HIGHSCORE': 'Only available for 1v1',
    }
    assert reasons_by_value['NONE'] == {
        'ONE_V_ONE': 'Only available for Highscore',
        'FREE_FOR_ALL': 'Only available for Highscore',
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
    assert 'data-label-base="Single Elimination"' in out
    assert 'data-label-base="None"' in out


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
):
    now = datetime(2026, 1, 1, 10, 0)
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
        + fragment
    )

    env = _make_env({'decision': template, 'macros/misc.html': _MACROS_MISC})
    env.globals['url_for'] = _decision_url_for
    # The fragment runs through `{%- endblock %}`, so it also includes
    # the trailing Reject `<details>` (rendered for 'submitted'/
    # 'accepted' requests) -- stub its two macros, since none of these
    # tests assert on the reject form itself.
    env.globals['form_field'] = lambda field, **kw: ''
    env.globals['form_buttons'] = lambda *a, **kw: ''
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
):
    tmpl = env.get_template('decision')
    user = SimpleNamespace(
        has_permission=lambda perm: (
            (can_create and perm == 'lan_tournament.create')
            or (can_decide and perm == 'lan_tournament.request_decide')
        )
    )
    if reject_form is None:
        # `errors=[]` mirrors a fresh, unsubmitted WTForms field --
        # the template's `{% if reject_form.reason.errors %}` (added
        # to auto-open the `<details>` for a re-rendered, erroneous
        # reject form) would otherwise hit `StrictUndefined` here,
        # since none of these tests care about the reject form itself.
        reject_form = SimpleNamespace(reason=SimpleNamespace(errors=[]))
    return tmpl.render(
        tournament_request=tournament_request,
        party=party,
        proposer_name=proposer_name,
        reject_form=reject_form,
        g=SimpleNamespace(user=user),
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


def test_view_request_decision_unchanged_for_live_linked_tournament(
    decision_env,
):
    """AC3: a live-linked `tournament_created` request (the negative
    case) renders neither the deleted note nor the re-create button --
    same (empty) Decision box as before this issue."""
    tournament_request = _make_real_request(
        status=TournamentRequestStatus.tournament_created,
        created_tournament_id=TournamentID(generate_uuid()),
    )
    party = SimpleNamespace(id='p1')

    out = _render_decision(
        decision_env,
        tournament_request=tournament_request,
        party=party,
        can_create=True,
    )

    assert 'has been deleted' not in out
    assert 'Create tournament again' not in out
    assert 'notification' not in out


# --------------------------------------------------------------------- #
# view_request.html -- reject <details> auto-open + preserved text
# --------------------------------------------------------------------- #

_MACROS_FORMS = """
{% macro form_field(field, class='', prefix=None, suffix=None, caption=None) -%}
<div class="form-control-block{% if field.errors %} invalid{% endif %}">
{{ field.label(class='form-label') }}
{{ field(class='form-control ' + class) }}
{%- for error in field.errors %}
<div class="form-error">{{ error }}</div>
{%- endfor %}
</div>
{%- endmacro %}

{% macro form_buttons(label, icon=None, color='primary', cancel_button=False, cancel_url=None) -%}
<button type="submit" class="button color-{{ color }}">{{ label }}</button>
{%- endmacro %}
"""


class _FakeReasonField:
    """Mimics just enough of a WTForms field for `form_field` to render it.

    Real `TournamentRequestRejectForm(request.form)` binding is
    exercised at the view-unit-test level
    (`test_tournament_request_views_admin.py`); this fake only needs
    to prove the *template* renders `field.data` back (preserves the
    typed text) and reacts to `field.errors`, the two things
    `form_field`'s real macro reads off the field.
    """

    def __init__(self, data, errors=()):
        self.data = data
        self.errors = list(errors)

    def label(self, **kwargs):
        return 'Reason'

    def __call__(self, **kwargs):
        return f'<textarea name="reason">{self.data}</textarea>'


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
        "{% from 'macros/forms.html' import form_field, form_buttons %}\n"
        + fragment
    )

    env = _make_env(
        {'reject_details': template, 'macros/forms.html': _MACROS_FORMS}
    )
    return env


def _render_reject_details(env, *, tournament_request, reject_form):
    tmpl = env.get_template('reject_details')
    return tmpl.render(
        tournament_request=tournament_request, reject_form=reject_form
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


# --------------------------------------------------------------------- #
# view_request.html -- history entry for the new event type
# --------------------------------------------------------------------- #


@pytest.fixture(scope='module')
def history_env():
    src = _VIEW_REQUEST_TEMPLATE.read_text()
    start = src.index('{# History #}')
    end = src.index('<div style="flex: 1 1 260px;">', start)
    fragment = src[start:end]

    env = _make_env({'history': fragment})
    env.filters['dateformat'] = lambda dt, *a, **k: dt.strftime('%Y-%m-%d')
    env.filters['timeformat'] = lambda dt, *a, **k: dt.strftime('%H:%M')
    return env


def _render_history(env, *, history, users_by_id=None):
    tmpl = env.get_template('history')
    return tmpl.render(history=history, users_by_id=users_by_id or {})


def test_view_request_history_shows_tournament_deleted_entry(history_env):
    entry = SimpleNamespace(
        event_type='tournament-request-tournament-deleted',
        occurred_at=datetime(2026, 3, 1, 9, 30),
        initiator_id=None,
        data={'tournament_id': 't1', 'tournament_name': 'Spring Cup'},
    )

    out = _render_history(history_env, history=[entry])

    assert 'Tournament deleted' in out
    assert 'Spring Cup' in out


def test_view_request_history_by_column_falls_back_for_deleted_initiator(
    history_env,
):
    """J2: a deleted initiator's `screen_name` is `None` and the user
    is still found in `users_by_id` -- printing it directly used to
    render the literal text "None" in the "By" column."""
    entry = SimpleNamespace(
        event_type='tournament-request-accepted',
        occurred_at=datetime(2026, 3, 1, 9, 30),
        initiator_id='u1',
        data={},
    )

    out = _render_history(
        history_env,
        history=[entry],
        users_by_id={'u1': SimpleNamespace(screen_name=None)},
    )

    assert 'Deleted user' in out
    assert 'None' not in out


def test_view_request_history_by_column_shows_screen_name_when_present(
    history_env,
):
    entry = SimpleNamespace(
        event_type='tournament-request-accepted',
        occurred_at=datetime(2026, 3, 1, 9, 30),
        initiator_id='u1',
        data={},
    )

    out = _render_history(
        history_env,
        history=[entry],
        users_by_id={'u1': SimpleNamespace(screen_name='Alice')},
    )

    assert 'Alice' in out
    assert 'Deleted user' not in out


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
    # rendered tag markup instead.
    assert out.count('Tournament deleted') == 1
    assert 'class="tag color-disabled">Tournament deleted' in out
    assert 'class="tag color-success">Tournament created' in out
