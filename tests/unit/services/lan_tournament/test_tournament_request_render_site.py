"""
tests.unit.services.lan_tournament.test_tournament_request_render_site
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Render the site tournament-request templates under ``StrictUndefined``.

`propose_form.html` and `my_requests.html` both `{% extends %}` the
lan_tournament site layout, so -- following `test_public_orga_display.py`
and `test_correction_panel_render.py` -- each is reduced to just its
`{% block body %}` content. The macros the stripped-out top-of-file
`{% from %}` imports would otherwise have pulled in (`form_field`,
`form_field_errors`, `form_buttons`, `render_icon`, `render_tag`) are
registered as environment globals instead, exactly as those two
existing tests do. `StrictUndefined` still catches a genuinely missing
context key or an un-`.get()`-guarded optional dict lookup -- the
concern the project's templating guardrail calls out -- since the
real template source is rendered untouched, just without a real
WTForms/Babel stack underneath it.
"""

from datetime import datetime
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


_PROPOSE_TEMPLATE = pathlib.Path(
    'byceps/services/lan_tournament/blueprints/site/templates'
    '/site/lan_tournament/propose_form.html'
)
_MY_REQUESTS_TEMPLATE = pathlib.Path(
    'byceps/services/lan_tournament/blueprints/site/templates'
    '/site/lan_tournament/my_requests.html'
)


def _snippet(path: pathlib.Path) -> str:
    """Return just the file's `{% block body %}` content.

    Strips the `{% extends %}` wrapper, the top-level `{% from %}`
    imports (`form_field`, `render_icon`, etc. are supplied as
    environment globals instead -- see `env` below, mirroring
    `test_correction_panel_render.py`) and the `{% set page_title %}`
    line, which references context keys not every mode supplies.
    """
    src = path.read_text()
    start = src.index('{% block body %}') + len('{% block body %}')
    end = src.rindex('{%- endblock %}')
    return src[start:end]


def _stub_form_field(field, **kwargs) -> Markup:
    caption = kwargs.get('caption')
    value = '' if field.data is None else field.data
    html = (
        f'<div class="form-control-block" id="{field.name}">'
        f'<label>{field.label.text}</label>'
        f'<input name="{field.name}" value="{value}">'
    )
    for err in field.errors:
        html += f'<span class="form-error">{err}</span>'
    if caption:
        html += f'<div class="form-caption">{caption}</div>'
    html += '</div>'
    # These stubs only ever see fixture strings this test module built
    # itself, standing in for macros whose real output Jinja already
    # treats as safe -- not user input.
    return Markup(html)  # noqa: S704


def _stub_form_field_errors(field) -> Markup:
    return Markup(  # noqa: S704
        ''.join(f'<span class="form-error">{e}</span>' for e in field.errors)
    )


def _stub_form_buttons(label, **kwargs) -> Markup:
    return Markup(  # noqa: S704
        f'<div class="button-row"><button type="submit">{label}</button></div>'
    )


@pytest.fixture(scope='module')
def env():
    e = Environment(
        undefined=StrictUndefined,
        autoescape=True,
        loader=DictLoader(
            {
                'propose_form': _snippet(_PROPOSE_TEMPLATE),
                'my_requests': _snippet(_MY_REQUESTS_TEMPLATE),
            }
        ),
    )
    e.globals['_'] = lambda s, **kw: s % kw if kw else s
    e.globals['url_for'] = lambda endpoint, **k: '/' + endpoint.lstrip('.')
    e.globals['render_icon'] = lambda *a, **k: ''
    e.globals['render_tag'] = lambda label, **k: Markup(  # noqa: S704
        f'<span class="tag {k.get("class", "")}">{label}</span>'
    )
    e.globals['form_field'] = _stub_form_field
    e.globals['form_field_errors'] = _stub_form_field_errors
    e.globals['form_buttons'] = _stub_form_buttons
    e.filters['dateformat'] = lambda dt, *a, **k: (
        dt.strftime('%Y-%m-%d') if dt else ''
    )
    e.filters['timeformat'] = lambda dt, *a, **k: (
        dt.strftime('%H:%M') if dt else ''
    )
    return e


# ------------------------------------------------------------------ #
# fakes
# ------------------------------------------------------------------ #


def _field(
    name, label_text, *, data=None, errors=(), choices=None, validators=None
):
    field = SimpleNamespace(
        name=name,
        label=SimpleNamespace(text=label_text),
        data=data,
        errors=list(errors),
    )
    if choices is not None:
        field.choices = choices
    if validators is not None:
        field.validators = validators
    return field


def _make_form(*, errors=None, elimination_mode_options=None):
    game_format_choices = [(fmt.value, fmt.label) for fmt in GameFormat]

    if elimination_mode_options is None:
        elimination_mode_options = [
            {
                'value': mode.value,
                'label': mode.name.title(),
                'disabled': False,
                'reason': None,
                'reasons': {fmt.value: None for fmt in GameFormat},
            }
            for mode in EliminationMode
        ]
    elimination_mode_choices = [
        (o['value'], o['label']) for o in elimination_mode_options
    ]

    return SimpleNamespace(
        name=_field('name', 'Name', data='Test Cup'),
        game=_field('game', 'Game', data='Test Game'),
        game_format=_field(
            'game_format',
            'Game format',
            data=GameFormat.ONE_V_ONE.value,
            choices=game_format_choices,
        ),
        elimination_mode=_field(
            'elimination_mode',
            'Elimination mode',
            data=EliminationMode.SINGLE_ELIMINATION.value,
            choices=elimination_mode_choices,
        ),
        team_size=_field('team_size', 'Team size', data=2),
        participant_limit=_field(
            'participant_limit',
            'Participant limit',
            data=8,
            validators=[SimpleNamespace(max=MAX_PARTICIPANT_LIMIT)],
        ),
        preferred_start_time=_field('preferred_start_time', 'Preferred start'),
        preferred_end_time=_field('preferred_end_time', 'Preferred end'),
        description=_field('description', 'Description', data='A description.'),
        special_rules=_field('special_rules', 'Special rules'),
        notes=_field('notes', 'Notes'),
        desired_template=_field('desired_template', 'Desired template'),
        elimination_mode_options=elimination_mode_options,
        errors=errors or {},
    )


def _make_tournament_request(
    *,
    number=1,
    name='Test Cup',
    rejection_reason=None,
    special_rules=None,
    notes=None,
    desired_template=None,
    tournament_deleted=False,
):
    now = SimpleNamespace(
        strftime=lambda fmt: '2026-06-01' if '%Y' in fmt else '10:00'
    )
    return SimpleNamespace(
        id='req-1',
        number=number,
        name=name,
        game='Test Game',
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        team_size=2,
        participant_limit=8,
        preferred_start_time=now,
        preferred_end_time=now,
        description='A description.',
        special_rules=special_rules,
        notes=notes,
        desired_template=desired_template,
        rejection_reason=rejection_reason,
        tournament_deleted=tournament_deleted,
    )


def _make_history_entry(event_type, *, occurred_at=None):
    now = occurred_at or SimpleNamespace(strftime=lambda fmt: '2026-06-01')
    return SimpleNamespace(occurred_at=now, event_type=event_type)


def _fake_request_row(
    status_value,
    *,
    number=1,
    name='Cup',
    rejection_reason=None,
    created_tournament_id=None,
):
    return SimpleNamespace(
        id=f'req-{number}',
        number=number,
        name=name,
        game='Test Game',
        game_format=SimpleNamespace(label='1v1'),
        status=SimpleNamespace(value=status_value),
        rejection_reason=rejection_reason,
        created_tournament_id=created_tournament_id,
    )


def _render_propose_form(env, **ctx):
    base_ctx = {
        'mode': 'create',
        'form': None,
        'tournament_request': None,
        'party_capacity': None,
        'history': None,
        'elimination_mode_label': 'Single Elimination',
    }
    base_ctx.update(ctx)
    return env.get_template('propose_form').render(**base_ctx)


def _render_my_requests(env, **ctx):
    base_ctx = {
        'requests': [],
        'status_counts': {},
        'open_requests': [],
        'archived_requests': [],
        'live_requests': [],
        'tournaments_by_request_id': {},
        'participant_counts': {},
        'draft_tournament_ids': set(),
    }
    base_ctx.update(ctx)
    return env.get_template('my_requests').render(**base_ctx)


# ------------------------------------------------------------------ #
# propose_form.html
# ------------------------------------------------------------------ #


def test_propose_form_renders_create_mode(env):
    form = _make_form()

    html = _render_propose_form(
        env, mode='create', form=form, party_capacity=100
    )

    assert 'I. What it is' in html
    assert 'II. Format' in html
    assert 'III. When' in html
    assert 'IV. If you like' in html
    assert 'id="game_format"' in html
    assert 'id="elimination_mode"' in html
    assert 'name="name"' in html
    assert 'request-danger-zone' not in html
    assert 'request-history' not in html


def test_propose_form_carries_server_max_limit_regardless_of_capacity(env):
    """Regression: the form must always carry the server-side hard
    cap (`data-max-limit`), whether or not the party's capacity is
    known, so the JS clamp never falls back to an unbounded max."""
    form = _make_form()

    with_capacity = _render_propose_form(
        env, mode='create', form=form, party_capacity=5000
    )
    without_capacity = _render_propose_form(
        env, mode='create', form=form, party_capacity=None
    )

    assert f'data-max-limit="{MAX_PARTICIPANT_LIMIT}"' in with_capacity
    assert f'data-max-limit="{MAX_PARTICIPANT_LIMIT}"' in without_capacity


def test_propose_form_renders_edit_mode(env):
    form = _make_form()
    tournament_request = _make_tournament_request(number=142)
    history = [_make_history_entry('tournament-request-submitted')]

    html = _render_propose_form(
        env,
        mode='edit',
        form=form,
        tournament_request=tournament_request,
        party_capacity=None,
        history=history,
    )

    assert 'Edit request' in html
    assert '#0142' in html
    assert 'request-danger-zone' in html
    assert '/withdraw_request' in html
    assert 'Request submitted' in html


def test_propose_form_renders_frozen_mode(env):
    tournament_request = _make_tournament_request(
        number=7, rejection_reason='Not enough interest.'
    )
    history = [_make_history_entry('tournament-request-rejected')]

    html = _render_propose_form(
        env,
        mode='frozen',
        form=None,
        tournament_request=tournament_request,
        party_capacity=None,
        history=history,
    )

    assert 'This request is no longer editable.' in html
    assert '#0007' in html
    assert 'Not enough interest.' in html
    assert 'Back to my requests' in html
    assert '<form' not in html


def test_propose_form_shows_disabled_elimination_modes_with_a_reason(env):
    """AC10: a disabled mode is struck through with its reason, never
    simply omitted from the markup."""
    options = [
        {
            'value': 'SINGLE_ELIMINATION',
            'label': 'Single Elimination',
            'disabled': False,
            'reason': None,
            'reasons': {
                'ONE_V_ONE': None,
                'FREE_FOR_ALL': None,
                'HIGHSCORE': 'Not available for Highscore',
            },
        },
        {
            'value': 'NONE',
            'label': 'None',
            'disabled': True,
            'reason': 'Only available for Highscore',
            'reasons': {
                'ONE_V_ONE': 'Only available for Highscore',
                'FREE_FOR_ALL': 'Only available for Highscore',
                'HIGHSCORE': None,
            },
        },
    ]
    form = _make_form(elimination_mode_options=options)

    html = _render_propose_form(env, mode='create', form=form)

    assert 'value="NONE"' in html
    assert 'disabled' in html
    assert 'is-disabled' in html
    assert 'Only available for Highscore' in html


def _elimination_mode_reasons_by_value(html: str) -> dict[str, dict]:
    """Map each elimination-mode radio's `value` to its parsed
    `data-reasons` JSON.

    A regex over the rendered `<input>` tags, not a substring check --
    AC1 requires asserting on the parsed JSON.
    """
    result = {}
    for tag in re.findall(r'<input\b[^>]*>', html):
        if 'name="elimination_mode"' not in tag:
            continue
        value_match = re.search(r'value="([^"]*)"', tag)
        reasons_match = re.search(r"data-reasons='([^']*)'", tag)
        assert value_match, f'no value attribute: {tag!r}'
        assert reasons_match, f'no data-reasons attribute: {tag!r}'
        result[value_match.group(1)] = json.loads(reasons_match.group(1))
    return result


def test_propose_form_elimination_mode_options_carry_reasons_json(env):
    """AC1 (workspace-dim0.15): every elimination-mode radio carries a
    `data-reasons` JSON mapping every `GameFormat` value to that mode's
    reason (`None` when valid), computed against the real domain
    service (`tournament_request_domain_service.allowed_elimination_modes`)
    -- this is the FIX-3 bug: without it, a fresh Highscore pick left
    every mode disabled, because the disabled state was only ever
    computed for the format the page loaded or posted with.
    """
    options = [
        {
            'value': 'SINGLE_ELIMINATION',
            'label': 'Single Elimination',
            'disabled': False,
            'reason': None,
            'reasons': {
                'ONE_V_ONE': None,
                'FREE_FOR_ALL': None,
                'HIGHSCORE': 'Not available for Highscore',
            },
        },
        {
            'value': 'ROUND_ROBIN',
            'label': 'Round Robin',
            'disabled': False,
            'reason': None,
            'reasons': {
                'ONE_V_ONE': None,
                'FREE_FOR_ALL': 'Only available for 1v1',
                'HIGHSCORE': 'Only available for 1v1',
            },
        },
        {
            'value': 'NONE',
            'label': 'None',
            'disabled': True,
            'reason': 'Only available for Highscore',
            'reasons': {
                'ONE_V_ONE': 'Only available for Highscore',
                'FREE_FOR_ALL': 'Only available for Highscore',
                'HIGHSCORE': None,
            },
        },
    ]
    form = _make_form(elimination_mode_options=options)

    html = _render_propose_form(env, mode='create', form=form)
    reasons_by_value = _elimination_mode_reasons_by_value(html)

    for option in options:
        assert reasons_by_value[option['value']] == option['reasons']

    # AC1's specific pin.
    assert reasons_by_value['NONE']['HIGHSCORE'] is None
    assert reasons_by_value['SINGLE_ELIMINATION']['ONE_V_ONE'] is None
    assert reasons_by_value['ROUND_ROBIN']['ONE_V_ONE'] is None


# ------------------------------------------------------------------ #
# my_requests.html
# ------------------------------------------------------------------ #


def test_my_requests_renders_all_five_statuses(env):
    submitted = _fake_request_row('submitted', number=1, name='Open Cup')
    accepted = _fake_request_row('accepted', number=2, name='Accepted Cup')
    created = _fake_request_row(
        'tournament_created',
        number=3,
        name='Live Cup',
        created_tournament_id='t-1',
    )
    rejected = _fake_request_row(
        'rejected',
        number=4,
        name='Rejected Cup',
        rejection_reason='Not enough interest.',
    )
    withdrawn = _fake_request_row('withdrawn', number=5, name='Withdrawn Cup')

    tournament = SimpleNamespace(
        id='t-1', name='Live Tournament', max_players=64
    )

    html = _render_my_requests(
        env,
        requests=[submitted, accepted, created, rejected, withdrawn],
        status_counts={
            'submitted': 1,
            'accepted': 1,
            'tournament_created': 1,
            'rejected': 1,
            'withdrawn': 1,
        },
        open_requests=[submitted, accepted],
        archived_requests=[rejected, withdrawn],
        live_requests=[created],
        tournaments_by_request_id={created.id: tournament},
        participant_counts={tournament.id: 23},
    )

    assert 'Open Cup' in html
    assert 'Accepted Cup' in html
    assert 'Live Tournament' in html
    assert 'Rejected Cup' in html
    assert 'Not enough interest.' in html
    assert 'Withdrawn Cup' in html
    assert '#0001' in html
    assert '23' in html and '64' in html
    assert 'is-current' in html  # tracker: submitted and accepted differ
    assert 'is-done' in html


def test_my_requests_renders_empty_state(env):
    html = _render_my_requests(env)

    assert 'You have not proposed any tournaments' in html
    assert 'Propose a tournament' in html
    assert 'request-status-counts' not in html
    assert 'request-card-grid' not in html


# ------------------------------------------------------------------ #
# deleted-tournament state (workspace-dim0.18)
# ------------------------------------------------------------------ #
#
# Unlike `_fake_request_row`/`_make_tournament_request` above (both
# `SimpleNamespace` stubs), these build a real `TournamentRequest`
# dataclass so `tournament_deleted` is the actual computed property,
# not a hand-set stub attribute.


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


def test_my_requests_in_the_program_shows_deleted_note(env):
    """AC1: the deleted-tournament card shows the proposer-facing note
    where the tournament link would be, using the `color-neutral`
    token (Issue 8)."""
    deleted_request = _make_real_request(
        status=TournamentRequestStatus.tournament_created,
        created_tournament_id=None,
    )

    html = _render_my_requests(
        env,
        requests=[deleted_request],
        live_requests=[deleted_request],
        tournaments_by_request_id={},
        participant_counts={},
    )

    assert (
        'The tournament created from your request has been deleted.' in html
    )
    assert 'color-neutral' in html
    # `status_counts`' always-present label makes a bare substring
    # check on "Tournament created" unreliable -- assert the specific
    # success-tag markup is absent from the live-request card instead.
    assert '<span class="tag color-success">Tournament created</span>' not in html


def test_my_requests_in_the_program_unchanged_for_live_linked_tournament(env):
    """AC3: the negative case -- a live-linked tournament -- still
    renders the tournament link, no deleted markers."""
    live_request = _make_real_request(
        status=TournamentRequestStatus.tournament_created,
        created_tournament_id=TournamentID(generate_uuid()),
    )
    tournament = SimpleNamespace(
        id='t-1', name='Live Tournament', max_players=None
    )

    html = _render_my_requests(
        env,
        requests=[live_request],
        live_requests=[live_request],
        tournaments_by_request_id={live_request.id: tournament},
        participant_counts={},
    )

    assert 'Live Tournament' in html
    assert 'has been deleted' not in html
    assert 'color-neutral' not in html


def test_propose_form_frozen_mode_shows_deleted_note(env):
    """AC1: the frozen-mode summary shows the proposer-facing note
    where the tournament link would be."""
    tournament_request = _make_real_request(
        status=TournamentRequestStatus.tournament_created,
        created_tournament_id=None,
    )

    html = _render_propose_form(
        env,
        mode='frozen',
        form=None,
        tournament_request=tournament_request,
        party_capacity=None,
        history=[],
    )

    assert (
        'The tournament created from your request has been deleted.' in html
    )


def test_propose_form_frozen_mode_hides_deleted_note_for_live_tournament(env):
    """AC3: the negative case -- a live-linked tournament -- shows no
    deleted note."""
    tournament_request = _make_real_request(
        status=TournamentRequestStatus.tournament_created,
        created_tournament_id=TournamentID(generate_uuid()),
    )

    html = _render_propose_form(
        env,
        mode='frozen',
        form=None,
        tournament_request=tournament_request,
        party_capacity=None,
        history=[],
    )

    assert 'has been deleted' not in html


def test_propose_form_frozen_mode_history_shows_tournament_deleted_entry(env):
    """History timelines: the new event type's label and the
    tournament name from `entry.data`."""
    tournament_request = _make_tournament_request(number=8)
    entry = SimpleNamespace(
        event_type='tournament-request-tournament-deleted',
        occurred_at=SimpleNamespace(strftime=lambda fmt: '2026-06-01'),
        data={'tournament_id': 't1', 'tournament_name': 'Spring Cup'},
    )

    html = _render_propose_form(
        env,
        mode='frozen',
        form=None,
        tournament_request=tournament_request,
        party_capacity=None,
        history=[entry],
    )

    assert 'Tournament deleted' in html
    assert 'Spring Cup' in html


def test_propose_form_edit_mode_history_shows_tournament_deleted_entry(env):
    """Same event-type label, exercised through the separate
    `event_labels` dict literal in the edit-mode history block."""
    form = _make_form()
    tournament_request = _make_tournament_request(number=8)
    entry = SimpleNamespace(
        event_type='tournament-request-tournament-deleted',
        occurred_at=SimpleNamespace(strftime=lambda fmt: '2026-06-01'),
        data={'tournament_id': 't1', 'tournament_name': 'Autumn Cup'},
    )

    html = _render_propose_form(
        env,
        mode='edit',
        form=form,
        tournament_request=tournament_request,
        party_capacity=None,
        history=[entry],
    )

    assert 'Tournament deleted' in html
    assert 'Autumn Cup' in html


# ------------------------------------------------------------------ #
# no-JS gaps (workspace-vxrc.3, issue k)
# ------------------------------------------------------------------ #


def test_propose_form_frozen_mode_shows_translated_elimination_mode_label(env):
    """k.2: the frozen summary must render the passed
    `elimination_mode_label`, never derive text from the raw enum
    member name itself. Uses a marker value that a naive
    `tournament_request.elimination_mode.name.replace('_', ' ')|title`
    could never produce (that always yields "Single Elimination" for
    this fixture's mode), so this actually distinguishes the two code
    paths rather than merely matching either one."""
    tournament_request = _make_tournament_request(number=3)
    assert tournament_request.elimination_mode is EliminationMode.SINGLE_ELIMINATION

    html = _render_propose_form(
        env,
        mode='frozen',
        form=None,
        tournament_request=tournament_request,
        party_capacity=None,
        history=[],
        elimination_mode_label='Einzelausscheidung (marker)',
    )

    assert 'Einzelausscheidung (marker)' in html
    assert 'Single Elimination' not in html
    assert 'SINGLE_ELIMINATION' not in html


def test_propose_form_error_summary_shows_field_label_not_raw_field_name(env):
    """k.3: the error summary's anchor text must be the field's label
    (`form[field_name].label.text`), not the raw snake_case field
    name."""
    form = _make_form(errors={'name': ['This field is required.']})

    html = _render_propose_form(env, mode='create', form=form)

    assert '>Name</a>' in html
    assert '>name</a>' not in html


def test_my_requests_draft_tournament_has_no_link_and_shows_label(env):
    """h: `my_requests` must never link a DRAFT tournament -- site
    `view` 404s it for everyone, proposer included. A DRAFT tournament
    shows its name plain plus an "In preparation" label instead."""
    created = _fake_request_row(
        'tournament_created', number=3, name='Live Cup', created_tournament_id='t-1'
    )
    tournament = SimpleNamespace(id='t-1', name='Live Cup', max_players=None)

    html = _render_my_requests(
        env,
        requests=[created],
        live_requests=[created],
        tournaments_by_request_id={created.id: tournament},
        participant_counts={},
        draft_tournament_ids={'t-1'},
    )

    assert 'href="/view"' not in html
    assert 'In preparation' in html
    assert 'Live Cup' in html


def test_my_requests_non_draft_tournament_keeps_link(env):
    """h negative case: a tournament not in `draft_tournament_ids`
    keeps its link, unchanged."""
    created = _fake_request_row(
        'tournament_created', number=4, name='Open Cup', created_tournament_id='t-2'
    )
    tournament = SimpleNamespace(id='t-2', name='Open Cup', max_players=None)

    html = _render_my_requests(
        env,
        requests=[created],
        live_requests=[created],
        tournaments_by_request_id={created.id: tournament},
        participant_counts={},
        draft_tournament_ids=set(),
    )

    assert 'href="/view"' in html
    assert 'In preparation' not in html

