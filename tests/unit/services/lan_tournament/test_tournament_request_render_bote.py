"""
tests.unit.services.lan_tournament.test_tournament_request_render_bote
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Render the totalverplant-36 "bote" overrides of the tournament-request
site templates (`propose_form.html`, `my_requests.html`) under
`StrictUndefined`, and prove the JS-hook contract (`data-capacity`,
`data-team`, `data-limit-inp` and friends -- see the epic's Wave 6
lead addendum) renders identically on both the generic base template
and its bote override.

Mirrors `test_tournament_request_render_site.py`: each template is
reduced to just its `{% block body %}` content and the macros its
top-of-file `{% from %}` imports would otherwise have pulled in
(`form_field`, `form_field_errors`, `form_buttons`, `render_icon`) are
supplied as environment globals instead. Unlike that sibling test,
`_stub_form_field` here reflects every extra keyword argument into a
real HTML attribute by delegating to the project's actually-installed
`wtforms.widgets.core.html_params` (not a hand-rolled lookalike), since
the hook contract under test lives entirely in those attributes -- and
that same delegation is what lets `test_h1_*` below prove the escaping
boundary a hand-rolled stub would have papered over.

This file owns its own render harness rather than importing the
sibling's, per its own file-ownership boundary.
"""

from datetime import datetime
from html import unescape as html_unescape
import json
import pathlib
import re
from types import SimpleNamespace

from jinja2 import DictLoader, Environment, StrictUndefined
from markupsafe import Markup
import pytest
from wtforms.widgets.core import html_params

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


_BASE_PROPOSE_TEMPLATE = pathlib.Path(
    'byceps/services/lan_tournament/blueprints/site/templates'
    '/site/lan_tournament/propose_form.html'
)

_BOTE_DIR = pathlib.Path(
    'sites/totalverplant-36/template_overrides/site/lan_tournament'
)
_BOTE_PROPOSE_TEMPLATE = _BOTE_DIR / 'propose_form.html'
_BOTE_MY_REQUESTS_TEMPLATE = _BOTE_DIR / 'my_requests.html'
_BOTE_STYLE_PARTIAL = _BOTE_DIR / '_bote_request_style.html'

_JS_PATH = pathlib.Path('byceps/static/behavior/lan_tournament_request.js')
_CSS_PATH = pathlib.Path('byceps/static/style/lan_tournament.css')


def _snippet(path: pathlib.Path) -> str:
    """Return just the file's `{% block body %}` content."""
    src = path.read_text()
    start = src.index('{% block body %}') + len('{% block body %}')
    end = src.rindex('{%- endblock %}')
    return src[start:end]


def _stub_form_field(field, **kwargs) -> Markup:
    """Stand in for `macros/forms.html`'s `form_field`.

    Reflects every extra kwarg into a real HTML attribute by calling
    the project's actually-installed `wtforms.widgets.core.html_params`
    -- the exact function the real `field(class=..., **kwargs)` call
    inside `form_field` uses -- rather than a hand-rolled lookalike.
    That means this stub also reproduces `html_params`'s use of
    `markupsafe.escape()`, which does *not* re-escape a value that is
    already `Markup` (it trusts `__html__` and returns it unchanged):
    exactly the boundary `test_h1_*` below exercises.
    """
    caption = kwargs.pop('caption', None)
    value = '' if field.data is None else field.data
    attrs = html_params(**kwargs)
    html = (
        f'<div class="form-control-block" id="{field.name}-block">'
        f'<label for="{field.name}">{field.label.text}</label>'
        f'<input id="{field.name}" name="{field.name}" '
        f'value="{value}"{" " + attrs if attrs else ""}>'
    )
    for err in field.errors:
        html += f'<span class="form-error">{err}</span>'
    if caption is not None:
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


def _make_env(templates: dict[str, str]) -> Environment:
    e = Environment(
        undefined=StrictUndefined,
        autoescape=True,
        loader=DictLoader(templates),
    )
    e.globals['_'] = lambda s, **kw: s % kw if kw else s
    e.globals['url_for'] = lambda endpoint, **k: (
        '/static/' + k['filename']
        if endpoint == 'static'
        else '/' + endpoint.lstrip('.')
    )
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


@pytest.fixture(scope='module')
def base_env() -> Environment:
    return _make_env({'propose_form': _snippet(_BASE_PROPOSE_TEMPLATE)})


@pytest.fixture(scope='module')
def bote_env() -> Environment:
    return _make_env(
        {
            'propose_form': _snippet(_BOTE_PROPOSE_TEMPLATE),
            'my_requests': _snippet(_BOTE_MY_REQUESTS_TEMPLATE),
        }
    )


# ------------------------------------------------------------------ #
# fakes (independent copies -- see module docstring)
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
    number=7,
    name='Test Cup',
    status='submitted',
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
        status=SimpleNamespace(value=status),
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
# JS-hook contract -- both surfaces, per the Wave 6 lead addendum
# ------------------------------------------------------------------ #


@pytest.mark.parametrize('env_name', ['base_env', 'bote_env'])
def test_hook_contract_renders_with_known_capacity(env_name, request):
    """AC11: `data-capacity`, the limit input's hook attributes and the
    script tag all render when `party_capacity` is known."""
    env = request.getfixturevalue(env_name)
    form = _make_form()

    html = _render_propose_form(
        env, mode='create', form=form, party_capacity=240
    )

    assert 'data-capacity="240"' in html
    assert f'data-max-limit="{MAX_PARTICIPANT_LIMIT}"' in html
    assert 'data-team' in html
    assert 'data-limit-inp' in html
    assert 'data-label-players="Participant limit"' in html
    assert 'data-label-teams="Team limit"' in html
    assert 'data-caption-template=' in html
    assert '{max}' in html
    assert 'lan_tournament_request.js' in html


@pytest.mark.parametrize('env_name', ['base_env', 'bote_env'])
def test_hook_contract_omits_capacity_when_unknown(env_name, request):
    """AC11: no `data-capacity` / `data-caption-template` when the
    party's capacity is unknown -- the label-swap attributes still
    render unconditionally."""
    env = request.getfixturevalue(env_name)
    form = _make_form()

    html = _render_propose_form(
        env, mode='create', form=form, party_capacity=None
    )

    assert 'data-capacity' not in html
    assert 'data-caption-template' not in html
    assert f'data-max-limit="{MAX_PARTICIPANT_LIMIT}"' in html
    assert 'data-team' in html
    assert 'data-limit-inp' in html
    assert 'data-label-players="Participant limit"' in html
    assert 'data-label-teams="Team limit"' in html


@pytest.mark.parametrize('env_name', ['base_env', 'bote_env'])
def test_hook_contract_renders_in_edit_mode_too(env_name, request):
    """The same form template serves create and edit; the hook must
    render either way since both are "the editable form"."""
    env = request.getfixturevalue(env_name)
    form = _make_form()
    tournament_request = _make_tournament_request(status='submitted')

    html = _render_propose_form(
        env,
        mode='edit',
        form=form,
        tournament_request=tournament_request,
        party_capacity=100,
        history=[],
    )

    assert 'data-capacity="100"' in html
    assert 'data-team' in html
    assert 'data-limit-inp' in html


# ------------------------------------------------------------------ #
# H1 (workspace-c9o4.4) -- Markup msgstrs must not break JS/attributes
# ------------------------------------------------------------------ #
#
# flask_babel's newstyle gettext returns `Markup`, which Jinja's
# autoescape then trusts and does not escape again. These stubs'
# `_` is a plain `str % kw` lambda everywhere else in this file, so it
# never exercises that boundary; `_markup_gettext` below does, by
# wrapping selected msgstrs in `Markup(...)` the way the real
# translator would.

_PROPOSE_FORM_PATH_BY_ENV = {
    'base_env': _BASE_PROPOSE_TEMPLATE,
    'bote_env': _BOTE_PROPOSE_TEMPLATE,
}


def _markup_gettext(overrides: dict[str, str]):
    """A gettext stub returning `Markup` for the given msgids (every
    other msgid passes through unchanged as a plain `str`), the way
    flask_babel's real newstyle gettext does under autoescape."""

    def _(s, **kw):
        text = overrides.get(s, s)
        rendered = (text % kw) if kw else text
        return Markup(rendered)  # noqa: S704

    return _


@pytest.mark.parametrize('env_name', ['base_env', 'bote_env'])
def test_h1_withdraw_confirm_survives_markup_msgstr_with_quotes(env_name):
    """A msgstr containing a `'` used to break the JS (`onsubmit`
    returned before the confirm dialog even opened -- withdraw
    submitted unconditionally); one containing a `"` broke out of the
    attribute entirely. The fix wraps the translated text in `|tojson`
    inside a single-quoted attribute -- prove a msgstr carrying every
    dangerous character (`'`, `"`, `<`, `>`, `&`) round-trips through
    it intact and never appears raw in the markup."""
    payload = 'Don\'t "escape" me <b>&</b>'
    env = _make_env(
        {'propose_form': _snippet(_PROPOSE_FORM_PATH_BY_ENV[env_name])}
    )
    env.globals['_'] = _markup_gettext(
        {'This will withdraw the request. Continue?': payload}
    )
    form = _make_form()
    tournament_request = _make_tournament_request(status='submitted')

    html = _render_propose_form(
        env,
        mode='edit',
        form=form,
        tournament_request=tournament_request,
        party_capacity=None,
        history=[],
    )

    match = re.search(r"onsubmit='return confirm\((.*?)\);'", html)
    assert match, html
    json_arg = match.group(1)
    # A syntactically valid JSON string that decodes back to the exact
    # translated text proves no character broke out of it.
    assert json.loads(json_arg) == payload
    assert payload not in html


@pytest.mark.parametrize('env_name', ['base_env', 'bote_env'])
def test_h1_participant_limit_label_attrs_escape_markup_msgstr(env_name):
    """`data_label_players` / `data_label_teams` / `data_caption_template`
    are passed to WTForms' `field(**kwargs)`, which builds attributes
    via `html_params` -> `markupsafe.escape()` -- and `escape()` does
    *not* re-escape a value that is already `Markup` (this file's
    `_stub_form_field` delegates to the real `html_params`, so it
    reproduces that trap faithfully). The fix wraps each value in
    `|forceescape`, which -- unlike a plain `escape()` call on an
    already-`Markup` value -- actually re-encodes it."""
    payload = 'Danger "quote" <img onerror=alert(1)>'
    env = _make_env(
        {'propose_form': _snippet(_PROPOSE_FORM_PATH_BY_ENV[env_name])}
    )
    env.globals['_'] = _markup_gettext({'Participant limit': payload})
    form = _make_form()

    html = _render_propose_form(
        env, mode='create', form=form, party_capacity=None
    )

    assert payload not in html
    match = re.search(r'data-label-players="([^"]*)"', html)
    assert match, html
    # The attribute value, HTML-unescaped, is exactly the translated
    # text -- if the fix were absent, the payload's own `"` would have
    # closed the attribute early and this would decode to a truncated,
    # mismatching string instead.
    assert html_unescape(match.group(1)) == payload


# ------------------------------------------------------------------ #
# H2 (workspace-c9o4.4) -- override propose_form lacked the nav highlight
# ------------------------------------------------------------------ #


def test_bote_propose_form_sets_current_page_in_same_position_as_siblings():
    """`index.html` and `my_requests.html` both set
    `{% set current_page = 'tournaments' %}` right after the
    `render_icon` import and before `page_title`; `propose_form.html`
    didn't. Static check on the exact same position/style."""
    src = _BOTE_PROPOSE_TEMPLATE.read_text()
    icons_import = "{% from 'macros/icons.html' import render_icon %}"
    current_page_set = "{% set current_page = 'tournaments' %}"

    assert icons_import in src
    assert current_page_set in src
    assert src.index(icons_import) < src.index(current_page_set)
    assert src.index(current_page_set) < src.index('{% set page_title')


def test_bote_propose_form_current_page_reaches_the_layout_body_class():
    """Render the real, full file (not the body-only snippet the rest
    of this module uses) through a minimal stand-in for
    `layout/base.html`'s `<body{% if current_page %}
    class="page-{{ current_page }}"{% endif %}>` line, to prove the
    child template's top-level `{% set current_page = 'tournaments' %}`
    actually reaches it at render time -- not just that the line is
    present in the source."""
    env = Environment(
        undefined=StrictUndefined,
        autoescape=True,
        loader=DictLoader(
            {
                'layout/base.html': (
                    '{%- set current_page = current_page|default -%}'
                    '<body{% if current_page %}'
                    ' class="page-{{ current_page }}"{% endif %}>'
                    '{% block head %}{% endblock %}'
                    '{% block body required %}{% endblock %}'
                    '</body>'
                ),
                'macros/forms.html': (
                    '{% macro form_field(field) %}{{ kwargs }}{% endmacro %}'
                    '{% macro form_field_errors(field) %}{% endmacro %}'
                    '{% macro form_buttons(label) %}{{ kwargs }}{% endmacro %}'
                ),
                'macros/icons.html': (
                    '{% macro render_icon(name) %}{% endmacro %}'
                ),
                'site/lan_tournament/_bote_request_style.html': '',
                'propose_form': _BOTE_PROPOSE_TEMPLATE.read_text(),
            }
        ),
    )
    env.globals['_'] = lambda s, **kw: s % kw if kw else s
    env.globals['url_for'] = lambda endpoint, **k: '/' + endpoint.lstrip('.')
    env.filters['dateformat'] = lambda dt, *a, **k: (
        dt.strftime('%Y-%m-%d') if dt else ''
    )
    env.filters['timeformat'] = lambda dt, *a, **k: (
        dt.strftime('%H:%M') if dt else ''
    )

    html = env.get_template('propose_form').render(
        mode='create',
        form=_make_form(),
        tournament_request=None,
        party_capacity=None,
        history=None,
        elimination_mode_label='Single Elimination',
    )

    assert 'class="page-tournaments"' in html


# ------------------------------------------------------------------ #
# bote propose_form.html
# ------------------------------------------------------------------ #


def test_bote_propose_form_renders_create_mode(bote_env):
    form = _make_form()

    html = _render_propose_form(
        bote_env, mode='create', form=form, party_capacity=100
    )

    assert 'class="fs"' in html
    assert 'class="seg"' in html
    assert 'class="opts"' in html
    assert 'id="game_format"' in html
    assert 'id="elimination_mode"' in html
    assert 'name="name"' in html
    assert 'stamp' not in html  # no page-header status while proposing
    assert 'desk-danger' not in html


def test_bote_propose_form_renders_edit_mode_with_desk_and_stamp(bote_env):
    form = _make_form()
    tournament_request = _make_tournament_request(
        number=142, status='submitted'
    )
    history = [_make_history_entry('tournament-request-submitted')]

    html = _render_propose_form(
        bote_env,
        mode='edit',
        form=form,
        tournament_request=tournament_request,
        party_capacity=None,
        history=history,
    )

    assert '0142' in html
    assert 'class="stamp stamp-submitted"' in html
    assert 'class="desk desk-danger"' in html
    assert '/withdraw_request' in html
    assert 'Request submitted' in html


def test_bote_propose_form_renders_frozen_mode_rejected(bote_env):
    tournament_request = _make_tournament_request(
        number=8, status='rejected', rejection_reason='Not enough interest.'
    )
    history = [_make_history_entry('tournament-request-rejected')]

    html = _render_propose_form(
        bote_env,
        mode='frozen',
        form=None,
        tournament_request=tournament_request,
        party_capacity=None,
        history=history,
    )

    assert 'class="stamp stamp-rejected"' in html
    assert '0008' in html
    assert 'Not enough interest.' in html
    assert '<form' not in html


def test_bote_propose_form_shows_disabled_elimination_modes_with_dis(
    bote_env,
):
    """Compromise: the bote override uses `.dis`, not the base
    layer's `is-disabled`, for a struck-through elimination mode."""
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

    html = _render_propose_form(bote_env, mode='create', form=form)

    assert 'value="NONE"' in html
    assert ' disabled' in html
    assert 'class="opt dis"' in html
    assert 'class="opt-label struck"' in html
    assert 'Only available for Highscore' in html


def _elimination_mode_reasons_by_value(html: str) -> dict[str, dict]:
    """Map each elimination-mode radio's `value` to its parsed
    `data-reasons` JSON. See the sibling helper in
    `test_tournament_request_render_site.py` (own harness, own copy,
    per this file's module docstring)."""
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


def test_bote_propose_form_elimination_mode_options_carry_reasons_json(
    bote_env,
):
    """AC1 (workspace-dim0.15): the bote override renders the same
    `data-reasons` JSON contract as the base template."""
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

    html = _render_propose_form(bote_env, mode='create', form=form)
    reasons_by_value = _elimination_mode_reasons_by_value(html)

    for option in options:
        assert reasons_by_value[option['value']] == option['reasons']

    assert reasons_by_value['NONE']['HIGHSCORE'] is None
    assert reasons_by_value['SINGLE_ELIMINATION']['ONE_V_ONE'] is None


# ------------------------------------------------------------------ #
# bote my_requests.html
# ------------------------------------------------------------------ #


def test_bote_my_requests_renders_all_five_statuses(bote_env):
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
        bote_env,
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

    # Hand-written `.st` tags, not render_tag (compromise C9).
    assert 'class="st st-submitted"' in html
    assert 'class="st st-accepted"' in html
    assert 'class="st st-tournament_created"' in html
    assert 'class="st st-rejected"' in html
    assert 'class="st st-withdrawn"' in html
    assert '<span class="tag' not in html

    # `.req.win` for the created tournament, `.trk` for the tracker.
    assert 'class="req win"' in html
    assert 'class="trk"' in html
    assert 'class="trk-step done"' in html
    assert 'class="trk-step now"' in html


def test_bote_my_requests_renders_empty_state(bote_env):
    html = _render_my_requests(bote_env)

    assert 'Propose a tournament' in html
    assert 'class="counts"' not in html
    assert 'class="req-grid"' not in html


# ------------------------------------------------------------------ #
# static checks -- AC1, AC3, AC8
# ------------------------------------------------------------------ #


@pytest.mark.parametrize(
    'path',
    [_BOTE_PROPOSE_TEMPLATE, _BOTE_MY_REQUESTS_TEMPLATE],
)
def test_bote_template_extends_base_layout_and_includes_style(path):
    """AC1: both overrides extend `layout/base.html` (not the module's
    own site layout) and include the style partial from the head
    block."""
    src = path.read_text()

    assert "{% extends 'layout/base.html' %}" in src
    assert '{% block head %}' in src
    head_start = src.index('{% block head %}')
    head_end = src.index('{%- endblock %}', head_start)
    head_block = src[head_start:head_end]
    assert (
        "{% include 'site/lan_tournament/_bote_request_style.html' %}"
        in head_block
    )


def test_bote_style_partial_has_no_jinja_looking_tokens_in_comments():
    """AC8: a `{% %}` / `{{ }}` sequence inside this included partial's
    CSS comments would parse and 500 the page (this has broken the
    codebase before). Assert none of the four token halves appear
    anywhere in the file at all, which is stricter than "in a
    comment" and therefore also proves it for the comments."""
    src = _BOTE_STYLE_PARTIAL.read_text()

    for token in ('{%', '%}', '{{', '}}'):
        assert token not in src, f'found Jinja-looking token {token!r}'


def test_bote_style_partial_has_no_hardcoded_hex_colours():
    """AC3: colours come from the theme's custom properties (--ink,
    --paper, --red, ... -- the same tokens `_bote_tournament_style.html`
    already uses), never a literal hex value."""
    src = _BOTE_STYLE_PARTIAL.read_text()

    assert not re.search(r'#[0-9a-fA-F]{3,8}', src)


def test_bote_style_partial_parses_as_a_jinja_template():
    """The partial is `{% include %}`d into real pages; a syntax error
    in it would 500 every page that includes it."""
    Environment(autoescape=True).parse(_BOTE_STYLE_PARTIAL.read_text())


# ------------------------------------------------------------------ #
# static checks -- lan_tournament.css / lan_tournament_request.js
# ------------------------------------------------------------------ #


def test_css_defines_color_neutral_token():
    """AC2: the site-only `color-neutral` status tag exists."""
    src = _CSS_PATH.read_text()

    assert '.tag.color-neutral' in src


def test_css_color_neutral_uses_only_lt_custom_properties():
    """Boundaries: never hard-code a hex value for a new token."""
    src = _CSS_PATH.read_text()
    start = src.index('.lt-dark .tag.color-neutral')
    end = src.index('}', start)
    rule = src[start:end]

    assert not re.search(r'#[0-9a-fA-F]{3,8}', rule)
    assert '--lt-' in rule


def test_js_swaps_participant_and_team_labels_from_data_attributes():
    """AC4: the script reads `data-label-players` / `data-label-teams`
    -- it must never hardcode either label as a string literal (AC12)."""
    src = _JS_PATH.read_text()

    assert 'data-label-players' in src
    assert 'data-label-teams' in src
    assert 'data-caption-template' in src
    assert 'data-capacity' in src


def test_js_has_no_user_facing_string_literals():
    """AC12, extended by workspace-dim0.15/AC3: the elimination-mode
    toggle must not hardcode any of the reason copy either -- every
    label and reason string comes from a data attribute."""
    src = _JS_PATH.read_text()

    assert not re.search(
        r'Teilnehmer|Team ?limit|Participant'
        r'|Highscore|Only available|Not available',
        src,
    )


def test_js_has_no_html_sink_calls():
    """AC3 (workspace-dim0.15): the elimination-mode toggle only ever
    uses `textContent`, never an HTML sink or `eval`."""
    src = _JS_PATH.read_text()

    assert not re.search(r'innerHTML|outerHTML|insertAdjacentHTML|eval\(', src)


def test_js_toggles_elimination_mode_data_reasons():
    """AC2 (workspace-dim0.15): the script reacts to `data-reasons` on
    `game_format` change and reselects the first valid mode."""
    src = _JS_PATH.read_text()

    assert 'data-reasons' in src
    assert 'data-mode-reason' in src
    assert 'data-label-base' in src
    assert "name !== 'game_format'" in src


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


def test_bote_propose_form_frozen_mode_shows_deleted_stamp_and_note(
    bote_env,
):
    """AC1: the hand-written stamp swaps to the deleted label and the
    note appears where the tournament link would be."""
    tournament_request = _make_real_request(
        status=TournamentRequestStatus.tournament_created,
        created_tournament_id=None,
    )

    html = _render_propose_form(
        bote_env,
        mode='frozen',
        form=None,
        tournament_request=tournament_request,
        party_capacity=None,
        history=[],
    )

    assert 'class="stamp stamp-tournament_deleted"' in html
    assert (
        'The tournament created from your request has been deleted.' in html
    )
    assert 'class="stamp stamp-tournament_created"' not in html


def test_bote_propose_form_frozen_mode_unchanged_for_live_tournament(
    bote_env,
):
    """AC3: the negative case -- a live-linked tournament -- keeps the
    plain `tournament_created` stamp and shows no note."""
    tournament_request = _make_real_request(
        status=TournamentRequestStatus.tournament_created,
        created_tournament_id=TournamentID(generate_uuid()),
    )

    html = _render_propose_form(
        bote_env,
        mode='frozen',
        form=None,
        tournament_request=tournament_request,
        party_capacity=None,
        history=[],
    )

    assert 'class="stamp stamp-tournament_created"' in html
    assert 'has been deleted' not in html


def test_bote_propose_form_frozen_mode_history_shows_tournament_deleted_entry(
    bote_env,
):
    tournament_request = _make_tournament_request(number=8)
    entry = SimpleNamespace(
        event_type='tournament-request-tournament-deleted',
        occurred_at=SimpleNamespace(strftime=lambda fmt: '2026-06-01'),
        data={'tournament_id': 't1', 'tournament_name': 'Spring Cup'},
    )

    html = _render_propose_form(
        bote_env,
        mode='frozen',
        form=None,
        tournament_request=tournament_request,
        party_capacity=None,
        history=[entry],
    )

    assert 'Tournament deleted' in html
    assert 'Spring Cup' in html


def test_bote_propose_form_edit_mode_history_shows_tournament_deleted_entry(
    bote_env,
):
    form = _make_form()
    tournament_request = _make_tournament_request(number=8)
    entry = SimpleNamespace(
        event_type='tournament-request-tournament-deleted',
        occurred_at=SimpleNamespace(strftime=lambda fmt: '2026-06-01'),
        data={'tournament_id': 't1', 'tournament_name': 'Autumn Cup'},
    )

    html = _render_propose_form(
        bote_env,
        mode='edit',
        form=form,
        tournament_request=tournament_request,
        party_capacity=None,
        history=[entry],
    )

    assert 'Tournament deleted' in html
    assert 'Autumn Cup' in html


def test_bote_my_requests_shows_deleted_state_for_deleted_tournament(
    bote_env,
):
    """AC1: the hand-written `.st` tag and note for a deleted
    tournament -- compromise C9, no `render_tag`."""
    deleted_request = _make_real_request(
        status=TournamentRequestStatus.tournament_created,
        created_tournament_id=None,
    )

    html = _render_my_requests(
        bote_env,
        requests=[deleted_request],
        live_requests=[deleted_request],
        tournaments_by_request_id={},
        participant_counts={},
    )

    assert 'class="st st-tournament_deleted"' in html
    assert (
        'The tournament created from your request has been deleted.' in html
    )
    assert 'class="st st-tournament_created"' not in html


def test_bote_my_requests_unchanged_for_live_linked_tournament(bote_env):
    """AC3: the negative case is unchanged."""
    live_request = _make_real_request(
        status=TournamentRequestStatus.tournament_created,
        created_tournament_id=TournamentID(generate_uuid()),
    )
    tournament = SimpleNamespace(
        id='t-1', name='Live Tournament', max_players=None
    )

    html = _render_my_requests(
        bote_env,
        requests=[live_request],
        live_requests=[live_request],
        tournaments_by_request_id={live_request.id: tournament},
        participant_counts={},
    )

    assert 'class="st st-tournament_created"' in html
    assert 'Live Tournament' in html
    assert 'has been deleted' not in html


# ------------------------------------------------------------------ #
# no-JS gaps (workspace-vxrc.3, issue k)
# ------------------------------------------------------------------ #


@pytest.mark.parametrize('env_name', ['base_env', 'bote_env'])
def test_error_summary_shows_field_label_not_raw_field_name(env_name, request):
    """k.3: both surfaces' error summary must anchor on the field's
    label text, not the raw snake_case field name."""
    env = request.getfixturevalue(env_name)
    form = _make_form(errors={'name': ['This field is required.']})

    html = _render_propose_form(env, mode='create', form=form)

    assert '>Name</a>' in html
    assert '>name</a>' not in html


def test_bote_propose_form_frozen_mode_shows_translated_elimination_mode_label(
    bote_env,
):
    """k.2: the bote frozen summary must render the passed
    `elimination_mode_label`, never derive text from the raw enum
    member name. Marker value a naive
    `.name.replace('_', ' ')|title` could never produce, so this
    actually distinguishes the two code paths."""
    tournament_request = _make_tournament_request(number=3, status='rejected')
    assert tournament_request.elimination_mode is EliminationMode.SINGLE_ELIMINATION

    html = _render_propose_form(
        bote_env,
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


def test_bote_my_requests_draft_tournament_has_no_link_and_shows_label(
    bote_env,
):
    """h: the bote override must never link a DRAFT tournament either."""
    created = _fake_request_row(
        'tournament_created', number=3, name='Live Cup', created_tournament_id='t-1'
    )
    tournament = SimpleNamespace(id='t-1', name='Live Cup', max_players=None)

    html = _render_my_requests(
        bote_env,
        requests=[created],
        live_requests=[created],
        tournaments_by_request_id={created.id: tournament},
        participant_counts={},
        draft_tournament_ids={'t-1'},
    )

    assert 'href="/view"' not in html
    assert 'In preparation' in html
    assert 'class="st st-draft"' in html
    assert 'Live Cup' in html


def test_bote_my_requests_non_draft_tournament_keeps_link(bote_env):
    """h negative case: unchanged for a non-DRAFT tournament."""
    created = _fake_request_row(
        'tournament_created', number=4, name='Open Cup', created_tournament_id='t-2'
    )
    tournament = SimpleNamespace(id='t-2', name='Open Cup', max_players=None)

    html = _render_my_requests(
        bote_env,
        requests=[created],
        live_requests=[created],
        tournaments_by_request_id={created.id: tournament},
        participant_counts={},
        draft_tournament_ids=set(),
    )

    assert 'href="/view"' in html
    assert 'In preparation' not in html
