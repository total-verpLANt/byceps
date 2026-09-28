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

import dataclasses
from datetime import datetime
from html import unescape as html_unescape
import json
import pathlib
import re
from types import SimpleNamespace

from flask import Flask
from flask_babel import Babel
from jinja2 import DictLoader, Environment, StrictUndefined
from markupsafe import Markup
import pytest
from wtforms import ValidationError
from wtforms.widgets.core import html_params

from byceps.services.lan_tournament.blueprints.site.forms import (
    TournamentProposeForm,
)
from byceps.services.lan_tournament.models.contestant_type import (
    ContestantType,
)
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
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
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


def _datetimeformat_stub(dt, fmt=None, **kwargs):
    """Stand in for Flask-Babel's `datetimeformat`."""
    if not dt:
        return ''
    return f'[{fmt}] {dt.strftime("%Y-%m-%d %H:%M")}'


def _make_env(templates: dict[str, str]) -> Environment:
    e = Environment(
        undefined=StrictUndefined,
        autoescape=True,
        loader=DictLoader(templates),
    )
    e.globals['_'] = lambda s, **kw: s % kw if kw else s
    e.globals['ngettext'] = lambda s, p, n, **kw: (
        (s if n == 1 else p) % dict(kw, num=n)
    )
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
    e.globals['g'] = SimpleNamespace(
        user=SimpleNamespace(authenticated=True),
        party=SimpleNamespace(title='total-verplant 36'),
    )
    e.filters['dateformat'] = lambda dt, *a, **k: (
        dt.strftime('%Y-%m-%d') if dt else ''
    )
    e.filters['timeformat'] = lambda dt, *a, **k: (
        dt.strftime('%H:%M') if dt else ''
    )
    e.filters['datetimeformat'] = _datetimeformat_stub
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


class _FakeField:
    """Independent stand-in for a WTForms field.

    Unlike the plain `SimpleNamespace` this file used to build, it is
    also *callable* -- `{{ field(class='form-control', ...) }}`, the
    real Jinja/WTForms field-call convention Issue 7's hand-written
    optional-field blocks use for `special_rules`/`notes`/
    `desired_template` -- and it always renders its own `id` attribute
    the way a real WTForms widget does, whether or not the caller
    passes one explicitly.
    """

    def __init__(
        self,
        name,
        label_text,
        *,
        data=None,
        errors=(),
        choices=None,
        validators=None,
        id=None,  # noqa: A002
        textarea=False,
    ):
        self.name = name
        self.textarea = textarea
        self.label = SimpleNamespace(text=label_text)
        self.data = data
        self.errors = list(errors)
        self.id = id or name
        if choices is not None:
            self.choices = choices
        if validators is not None:
            self.validators = validators

    def __call__(self, **kwargs) -> Markup:
        kwargs.setdefault('id', self.id)
        value = '' if self.data is None else self.data
        attrs = html_params(**kwargs)
        if self.textarea:
            return Markup(  # noqa: S704
                f'<textarea name="{self.name}"'
                f'{" " + attrs if attrs else ""}>{value}</textarea>'
            )
        return Markup(  # noqa: S704
            f'<input name="{self.name}" value="{value}"'
            f'{" " + attrs if attrs else ""}>'
        )


def _field(
    name,
    label_text,
    *,
    data=None,
    errors=(),
    choices=None,
    validators=None,
    id=None,  # noqa: A002
    textarea=False,
):
    return _FakeField(
        name,
        label_text,
        data=data,
        errors=errors,
        choices=choices,
        validators=validators,
        id=id,
        textarea=textarea,
    )


_GAME_FORMAT_OPTIONS = [
    {
        'value': GameFormat.ONE_V_ONE.value,
        'label': 'One on one',
        'subtitle': 'Duel',
    },
    {
        'value': GameFormat.FREE_FOR_ALL.value,
        'label': 'Free-for-all',
        'subtitle': 'Free-for-All',
    },
    {
        'value': GameFormat.HIGHSCORE.value,
        'label': 'Highscore',
        'subtitle': 'Ranking',
    },
]


def _make_form(
    *, errors=None, elimination_mode_options=None, game_format_options=None
):
    game_format_choices = [(fmt.value, fmt.label) for fmt in GameFormat]

    if elimination_mode_options is None:
        elimination_mode_options = [
            {
                'value': mode.value,
                'label': mode.name.title(),
                'disabled': False,
                'reason': None,
                'reasons': {fmt.value: None for fmt in GameFormat},
                'description': f'{mode.name.title()} description.',
            }
            for mode in EliminationMode
        ]
    if game_format_options is None:
        game_format_options = _GAME_FORMAT_OPTIONS
    elimination_mode_choices = [
        (o['value'], o['label']) for o in elimination_mode_options
    ]

    form = SimpleNamespace(
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
        description=_field(
            'description',
            'Description',
            data='A description.',
            textarea=True,
        ),
        special_rules=_field('special_rules', 'Special rules', textarea=True),
        notes=_field(
            'notes', 'Notes for the orga', id='request_notes', textarea=True
        ),
        desired_template=_field('desired_template', 'Desired template'),
        elimination_mode_options=elimination_mode_options,
        game_format_options=game_format_options,
        errors=errors or {},
    )
    form.error_summary = lambda: [
        (getattr(form, name).id, getattr(form, name).label.text, error)
        for name, field_errors in form.errors.items()
        for error in field_errors
    ]
    return form


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
        created_at=now,
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
        decided_at=now,
        rejection_reason=rejection_reason,
        tournament_deleted=tournament_deleted,
    )


def _make_history_entry(event_type, *, occurred_at=None):
    now = occurred_at or SimpleNamespace(strftime=lambda fmt: '2026-06-01')
    return SimpleNamespace(occurred_at=now, event_type=event_type)


def _fake_dt(date_str='2026-06-01', time_str='10:00'):
    """A minimal stand-in with just enough of `datetime` (`strftime`)
    for the `dateformat`/`timeformat` stub filters above."""
    return SimpleNamespace(
        strftime=lambda fmt: date_str if '%Y' in fmt else time_str
    )


def _fake_request_row(
    status_value,
    *,
    number=1,
    name='Cup',
    rejection_reason=None,
    created_tournament_id=None,
    preferred_start_time=None,
    created_at=None,
    decided_at=None,
    updated_at=None,
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
        preferred_start_time=preferred_start_time or _fake_dt(),
        created_at=created_at or _fake_dt(),
        decided_at=decided_at or _fake_dt(),
        updated_at=updated_at,
    )


def _fake_tournament(tournament_id='t-1', **overrides):
    fields = {
        'id': tournament_id,
        'name': 'Live Tournament',
        'max_players': None,
        'max_teams': None,
        'start_time': None,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _render_propose_form(env, **ctx):
    base_ctx = {
        'mode': 'create',
        'form': None,
        'tournament_request': None,
        'party_capacity': None,
        'history': None,
        'elimination_mode_label': 'Single Elimination',
        'format_label': 'One on one',
        'request_mode_label': 'Single knockout',
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
        'signup_counts': {},
        'reason_leads_by_request_id': {},
        'draft_tournament_ids': set(),
        'orga_tournament_ids': set(),
        'waiting_days_by_request_id': {},
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
# H1 (workspace-pv3b.23) -- `_ParticipantLimitRange` must not leak a
# raw `%(min)s` placeholder into the rendered page
# ------------------------------------------------------------------ #


@pytest.fixture(scope='module')
def participant_limit_errors() -> tuple[str, str]:
    """The real below-min/above-max `_ParticipantLimitRange` messages,
    taken from the actual validator on `TournamentProposeForm` -- not
    hand-typed -- so a regression that drops the `% dict(...)`
    interpolation (fix 2 -> fix 3, workspace-pv3b.23) shows up here as
    a literal `%(min)s` too, not just in the form-level unit test."""
    app = Flask(__name__)
    app.config['TESTING'] = True
    app.config['LOCALE'] = 'en'
    app.config['BABEL_DEFAULT_LOCALE'] = 'en'
    Babel(app)
    with app.test_request_context('/'):
        validator = next(
            v
            for v in TournamentProposeForm().participant_limit.validators
            if hasattr(v, 'min') and hasattr(v, 'max')
        )
        try:
            validator(None, SimpleNamespace(data=1))
            below_min = None
        except ValidationError as e:
            below_min = str(e)
        try:
            validator(None, SimpleNamespace(data=5000))
            above_max = None
        except ValidationError as e:
            above_max = str(e)
    assert below_min and above_max
    return below_min, above_max


@pytest.mark.parametrize('mode', ['create', 'edit'])
@pytest.mark.parametrize('env_name', ['base_env', 'bote_env'])
def test_propose_form_participant_limit_errors_have_no_raw_placeholder(
    env_name, mode, request, participant_limit_errors
):
    """Fix 2 raised `ValidationError(self.min_message)` without
    interpolating it, so a limit of `1` rendered "Enter at least
    %(min)s." verbatim. Feed the real, current validator output
    through the actual template on both surfaces and both modes and
    prove no `%(` placeholder survives to the page."""
    env = request.getfixturevalue(env_name)
    below_min, above_max = participant_limit_errors
    form = _make_form()
    form.participant_limit.errors = [below_min, above_max]

    ctx = {'mode': mode, 'form': form, 'party_capacity': 100}
    if mode == 'edit':
        ctx['tournament_request'] = _make_tournament_request(status='submitted')
        ctx['history'] = []

    html = _render_propose_form(env, **ctx)

    assert '%(' not in html
    assert below_min in html
    assert above_max in html


# ------------------------------------------------------------------ #
# H2 (workspace-c9o4.4) -- override propose_form lacked the nav highlight
# ------------------------------------------------------------------ #


def test_bote_propose_form_sets_current_page_in_same_position_as_siblings():
    """`propose_form.html` sets `current_page` like the other pages."""
    src = _BOTE_PROPOSE_TEMPLATE.read_text()
    forms_import = "{% from 'macros/forms.html' import"
    current_page_set = "{% set current_page = 'tournaments' %}"

    assert forms_import in src
    assert current_page_set in src
    assert src.index(forms_import) < src.index(current_page_set)
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
    env.filters['datetimeformat'] = _datetimeformat_stub

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


# ------------------------------------------------------------------ #
# ticket gate notice (workspace-pv3b.5 / 4o71)
# ------------------------------------------------------------------ #


def test_bote_propose_form_shows_ticket_notice_when_ticketless(bote_env):
    form = _make_form()

    html = _render_propose_form(
        bote_env, mode='create', form=form, has_ticket=False
    )

    assert 'class="warn"' in html
    assert 'Ticket required' in html
    assert 'You need a ticket for this party to propose a tournament.' in html
    assert 'disabled' in html


def test_bote_propose_form_hides_ticket_notice_with_ticket(bote_env):
    form = _make_form()

    html = _render_propose_form(
        bote_env, mode='create', form=form, has_ticket=True
    )

    assert 'class="warn"' not in html
    assert 'Ticket required' not in html
    assert 'disabled' not in html


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
    assert 'class="stamp st-submitted"' in html
    assert 'class="danger-zone"' in html
    assert '/withdraw_request' in html
    assert '<div class="desk-h">History</div>' in html
    assert 'Submitted' in html


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

    assert 'class="stamp st-rejected"' in html
    assert '0008' in html
    assert 'Not enough interest.' in html
    assert '<form' not in html


def test_bote_propose_form_frozen_mode_does_not_repeat_the_free_text(
    bote_env,
):
    """The frozen summary is the draft's six `.sup` rows."""
    tournament_request = _make_tournament_request(
        number=9,
        status='accepted',
        notes='Please keep it quiet after midnight.',
        special_rules='No blue shells.',
        desired_template='like last year',
    )

    html = _render_propose_form(
        bote_env,
        mode='frozen',
        form=None,
        tournament_request=tournament_request,
        party_capacity=None,
        history=[],
    )

    assert 'Notes for the orga' not in html
    assert 'Please keep it quiet after midnight.' not in html
    assert 'No blue shells.' not in html
    assert 'like last year' not in html
    assert 'A description.' not in html


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
            'description': 'Whoever loses is out.',
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
            'description': 'Leaderboard only, no rounds.',
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
            'description': 'Whoever loses is out.',
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
            'description': 'Leaderboard only, no rounds.',
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
# workspace-pv3b.7 -- draft-conformant form (create/edit/frozen)
# ------------------------------------------------------------------ #


def test_bote_propose_form_section_heads_carry_required_tags(bote_env):
    """AC1: sections I-III are tagged "Required", section IV "Can
    stay empty" -- the draft's per-section commitment tag."""
    form = _make_form()

    html = _render_propose_form(bote_env, mode='create', form=form)

    assert '<em>I</em>' in html
    assert '<em>II</em>' in html
    assert '<em>III</em>' in html
    assert '<em>IV</em>' in html
    assert html.count('Required') == 3
    assert 'Can stay empty' in html


def test_bote_propose_form_renders_format_subtitles_and_mode_descriptions(
    bote_env,
):
    """AC1: the segmented control shows each game format's subtitle
    (not just its raw enum label), and each elimination-mode option
    shows its one-line description."""
    form = _make_form()

    html = _render_propose_form(bote_env, mode='create', form=form)

    for option in _GAME_FORMAT_OPTIONS:
        assert option['label'] in html
        assert option['subtitle'] in html
    for option in form.elimination_mode_options:
        assert option['description'] in html


def test_bote_propose_form_marks_optional_fields_and_placeholders(bote_env):
    """AC2: the three optional fields carry the "optional" chip and
    the draft's placeholders/captions."""
    form = _make_form()

    html = _render_propose_form(bote_env, mode='create', form=form)

    assert html.count('class="opt-chip"') == 3
    assert html.count('optional') >= 3
    assert 'placeholder="e.g. Rollator-Rallye 2026"' in html
    assert 'placeholder="Only the orga sees this."' in html
    assert 'Never shown publicly.' in html
    assert (
        'Shown publicly on the tournament page. Max. 2000 characters.' in html
    )
    assert '1 = solo tournament' in html


def test_bote_propose_form_uses_draft_buttons_without_core_colours(bote_env):
    """AC2/AC7: create, edit and the danger zone all use the
    hand-written draft buttons -- never a core `color-*` fill or the
    `form_buttons` macro."""
    create_html = _render_propose_form(
        bote_env, mode='create', form=_make_form()
    )
    edit_html = _render_propose_form(
        bote_env,
        mode='edit',
        form=_make_form(),
        tournament_request=_make_tournament_request(status='submitted'),
        history=[],
    )

    for html in (create_html, edit_html):
        assert 'class="button btn-pri"' in html
        assert 'class="lnk"' in html
        assert 'color-' not in html
        assert 'form_buttons(' not in html

    assert 'class="button btn-sm out-red"' in edit_html
    assert 'class="danger-zone"' in edit_html


def test_bote_propose_form_error_summary_links_fields(bote_env):
    """AC3: the error summary counts every field error (not just the
    number of fields) and links each one to its field by id."""
    form = _make_form(
        errors={
            'name': ['This field is required.'],
            'preferred_end_time': [
                'Preferred end time must not precede start time.'
            ],
        }
    )
    form.name.errors = ['This field is required.']
    form.preferred_end_time.errors = [
        'Preferred end time must not precede start time.'
    ]

    html = _render_propose_form(bote_env, mode='create', form=form)

    assert 'class="warn-h"' in html
    assert '2 entries to check' in html
    assert '<a href="#name">Name</a> This field is required.' in html
    assert (
        '<a href="#preferred_end_time">Preferred end</a> '
        'Preferred end time must not precede start time.' in html
    )


def test_bote_edit_mode_note_box_danger_zone_and_timeline(bote_env):
    """AC4: edit mode shows the ochre "not decided yet" note box, the
    danger zone (not a `.desk`) and a `.tl` timeline that names changed
    fields and ends with a synthetic "now" entry."""
    form = _make_form()
    tournament_request = _make_tournament_request(
        number=142, status='submitted'
    )
    history = [
        _make_history_entry('tournament-request-submitted'),
        SimpleNamespace(
            event_type='tournament-request-edited',
            occurred_at=SimpleNamespace(strftime=lambda fmt: '2026-06-01'),
            data={'changed_fields': ['special_rules']},
        ),
    ]

    html = _render_propose_form(
        bote_env,
        mode='edit',
        form=form,
        tournament_request=tournament_request,
        party_capacity=None,
        history=history,
    )

    assert 'class="note-box"' in html
    assert 'class="note-box lock"' not in html
    assert 'The orga has not decided yet' in html
    assert 'class="danger-zone"' in html
    assert 'Withdraw this request' in html
    assert 'class="tl"' in html
    assert 'class="lbl"' in html
    assert 'Special rules' in html
    assert 'class="now"' in html
    assert 'Waiting for the orga' in html


def test_bote_frozen_mode_lock_note(bote_env):
    """AC4: frozen mode shows the blue "Frozen" lock note, no `<form>`
    and the full-width back button."""
    tournament_request = _make_tournament_request(number=139, status='accepted')

    html = _render_propose_form(
        bote_env,
        mode='frozen',
        form=None,
        tournament_request=tournament_request,
        party_capacity=None,
        history=[],
    )

    assert 'class="note-box lock"' in html
    assert 'Frozen' in html
    assert '<form' not in html
    assert 'class="button btn-full"' in html


# ------------------------------------------------------------------ #
# F1 (fix cycle 1, Issue 7) -- frozen note must be status-true
# ------------------------------------------------------------------ #


def test_bote_frozen_mode_is_status_true_for_rejected(bote_env):
    """F1: a rejected request must not show the "accepted" lock note
    -- it shows the rejected vocabulary class and its own wording."""
    tournament_request = _make_tournament_request(
        number=8, status='rejected', rejection_reason='Not enough interest.'
    )

    html = _render_propose_form(
        bote_env,
        mode='frozen',
        form=None,
        tournament_request=tournament_request,
        party_capacity=None,
        history=[],
    )

    assert 'The orga accepted your request' not in html
    assert 'class="st st-rejected"' in html
    assert 'class="note-box lock"' not in html
    assert 'The orga rejected your request.' in html
    assert 'Not enough interest.' in html


def test_bote_frozen_rejected_shows_reason_only_once(bote_env):
    """B4 (workspace-pv3b.22): the rejection reason used to appear
    both in the note-box and in the summary's "Rejection reason" `dl`
    entry. It must render exactly once -- in the note-box, matching
    how `accepted`/`withdrawn` already show their own message only
    there, never duplicated into the summary."""
    tournament_request = _make_tournament_request(
        number=10, status='rejected', rejection_reason='Not enough interest.'
    )

    html = _render_propose_form(
        bote_env,
        mode='frozen',
        form=None,
        tournament_request=tournament_request,
        party_capacity=None,
        history=[],
    )

    assert html.count('Not enough interest.') == 1
    assert 'Rejection reason' not in html


def test_bote_frozen_mode_is_status_true_for_withdrawn(bote_env):
    """F1: a withdrawn request must not show the "accepted" lock
    note -- it shows the withdrawn vocabulary class and its own
    wording."""
    tournament_request = _make_tournament_request(number=9, status='withdrawn')

    html = _render_propose_form(
        bote_env,
        mode='frozen',
        form=None,
        tournament_request=tournament_request,
        party_capacity=None,
        history=[],
    )

    assert 'The orga accepted your request' not in html
    assert 'class="st st-withdrawn"' in html
    assert 'class="note-box lock"' not in html
    assert 'You withdrew this request.' in html


def test_bote_frozen_mode_is_status_true_for_tournament_created(bote_env):
    """F1: an accepted-turned-`tournament_created` request must not
    show the "accepted" lock note -- it shows the `tournament_created`
    vocabulary class, its own wording, and a link to the tournament."""
    tournament_id = TournamentID(generate_uuid())
    tournament_request = _make_real_request(
        status=TournamentRequestStatus.tournament_created,
        created_tournament_id=tournament_id,
    )

    html = _render_propose_form(
        bote_env,
        mode='frozen',
        form=None,
        tournament_request=tournament_request,
        created_tournament=_make_tournament_stub(
            tournament_id, TournamentStatus.REGISTRATION_OPEN
        ),
        party_capacity=None,
        history=[],
    )

    assert 'The orga accepted your request' not in html
    assert 'class="st st-tournament_created"' in html
    assert 'class="note-box lock"' not in html
    assert 'The orga created the tournament from your request.' in html
    assert 'To the tournament' in html


def test_bote_frozen_created_with_draft_tournament_shows_in_preparation_not_link(
    bote_env,
):
    """G1: the site `view()` 404s DRAFT tournaments, so the frozen
    note for a create-from-request DRAFT must show the same
    "In preparation" tag `my_requests.html` uses instead of a link
    that would 404."""
    tournament_id = TournamentID(generate_uuid())
    tournament_request = _make_real_request(
        status=TournamentRequestStatus.tournament_created,
        created_tournament_id=tournament_id,
    )

    html = _render_propose_form(
        bote_env,
        mode='frozen',
        form=None,
        tournament_request=tournament_request,
        created_tournament=_make_tournament_stub(
            tournament_id, TournamentStatus.DRAFT
        ),
        party_capacity=None,
        history=[],
    )

    assert 'class="st st-draft"' in html
    assert 'In preparation' in html
    assert 'To the tournament' not in html
    assert f'tournament_id={tournament_id}' not in html


def test_bote_frozen_created_with_open_tournament_links_it(bote_env):
    """G1: a non-DRAFT created tournament still gets the link -- the
    site `view()` serves it fine."""
    tournament_id = TournamentID(generate_uuid())
    tournament_request = _make_real_request(
        status=TournamentRequestStatus.tournament_created,
        created_tournament_id=tournament_id,
    )

    html = _render_propose_form(
        bote_env,
        mode='frozen',
        form=None,
        tournament_request=tournament_request,
        created_tournament=_make_tournament_stub(
            tournament_id, TournamentStatus.REGISTRATION_OPEN
        ),
        party_capacity=None,
        history=[],
    )

    assert 'To the tournament' in html
    assert 'class="st st-draft"' not in html


def test_bote_frozen_created_with_none_status_tournament_links_it(bote_env):
    """C2 (workspace-pv3b.25): `created_tournament.tournament_status`
    can be `None` (`_safe_enum_lookup` returns `None` for a row the
    mapping cannot classify), and `.name` on it used to raise
    `UndefinedError` under `StrictUndefined`. The site `view()` only
    404s when the status IS DRAFT (`tournament.tournament_status and
    tournament.tournament_status == TournamentStatus.DRAFT`) -- a
    `None` status is served fine -- so the template must link it too,
    the same as any other non-DRAFT status."""
    tournament_id = TournamentID(generate_uuid())
    tournament_request = _make_real_request(
        status=TournamentRequestStatus.tournament_created,
        created_tournament_id=tournament_id,
    )

    html = _render_propose_form(
        bote_env,
        mode='frozen',
        form=None,
        tournament_request=tournament_request,
        created_tournament=_make_tournament_stub(tournament_id, None),
        party_capacity=None,
        history=[],
    )

    assert 'To the tournament' in html
    assert 'class="st st-draft"' not in html
    assert 'In preparation' not in html


def test_bote_partial_status_vocabulary_matches_draft():
    """AC5: the shared status vocabulary matches the draft -- open is
    ochre, accepted is blue, tournament_created is a filled green tag,
    never the old redDeep/green outline pair."""
    src = _BOTE_STYLE_PARTIAL.read_text()

    assert '--ochre: #7a5410' in src

    submitted_start = src.index('.st-submitted')
    submitted_rule = src[submitted_start : src.index('}', submitted_start) + 1]
    assert 'var(--ochre)' in submitted_rule

    accepted_start = src.index('.st-accepted')
    accepted_rule = src[accepted_start : src.index('}', accepted_start) + 1]
    assert 'var(--blue)' in accepted_rule

    created_start = src.index('.st-tournament_created')
    created_rule = src[created_start : src.index('}', created_start) + 1]
    assert 'background: var(--green)' in created_rule
    assert 'color: var(--paperLite)' in created_rule


def test_bote_request_slip_has_carbon_offset_and_mobile_reset():
    """AC6: Papiergrund Patch §4, verbatim -- the carbon-copy offset,
    the clamped desk margin and the ≤720px reset to no shadow/margin."""
    src = _BOTE_STYLE_PARTIAL.read_text()

    assert '10px 10px 0 0 var(--inkMute)' in src
    assert 'margin: clamp(14px, 2vw, 28px) auto' in src
    assert '@media (max-width: 720px)' in src
    mobile_start = src.index('@media (max-width: 720px)')
    mobile_rule = src[mobile_start : src.index('}', mobile_start) + 1]
    assert 'box-shadow: none' in mobile_rule


def test_bote_request_pages_use_draft_header_without_folio(bote_env):
    """AC6: create, edit and `my_requests` all render the draft header
    (breadcrumbs, kicker, sub) and never the old `.page-folio` strip."""
    create_html = _render_propose_form(
        bote_env, mode='create', form=_make_form()
    )
    edit_html = _render_propose_form(
        bote_env,
        mode='edit',
        form=_make_form(),
        tournament_request=_make_tournament_request(status='submitted'),
        history=[],
    )
    my_requests_html = _render_my_requests(bote_env)

    for html in (create_html, edit_html, my_requests_html):
        assert 'class="crumbs"' in html
        assert 'class="kick"' in html
        assert 'class="sub"' in html
        assert 'page-folio' not in html


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

    tournament = _fake_tournament(max_players=64)

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
        signup_counts={tournament.id: (23, 64)},
        reason_leads_by_request_id={
            rejected.id: ('Not enough interest.', False)
        },
    )

    assert 'Open Cup' in html
    assert 'Accepted Cup' in html
    assert 'Live Tournament' in html
    assert 'Rejected Cup' in html
    assert '„Not enough interest.“' in html
    assert 'Withdrawn Cup' in html
    assert '23' in html and '64' in html

    # Hand-written `.st` tags, not render_tag (compromise C9).
    assert 'class="st st-submitted"' in html
    assert 'class="st st-accepted"' in html
    assert 'class="st st-rejected"' in html
    assert 'class="st st-withdrawn"' in html
    assert '<span class="tag' not in html

    # `.req.win` for the created tournament, `.trk` for the tracker --
    # the new tracker's `<li>` classes are bare `done`/`next` (AC7: no
    # `trk-step`, no filled ink bar for the pending third step).
    assert 'class="req win"' in html
    assert 'class="trk"' in html
    assert 'class="done"' in html
    assert 'class="next"' in html
    assert 'aria-current="step"' in html


def test_bote_my_requests_cta_uses_btn_pri(bote_env):
    """AC3: the header CTA (and the empty-state one) use the draft's
    hand-written `btn-pri` button, never a core `color-*` fill."""
    submitted = _fake_request_row('submitted', number=1, name='Open Cup')

    html = _render_my_requests(
        bote_env,
        requests=[submitted],
        status_counts={'submitted': 1},
        open_requests=[submitted],
    )

    assert 'class="button btn-pri btn-full"' in html
    assert 'color-info' not in html
    assert 'color-primary' not in html

    empty_html = _render_my_requests(bote_env)
    assert 'class="button btn-pri"' in empty_html


def test_bote_my_requests_card_shows_start_hint_and_actions(bote_env):
    """AC1: the open card shows the preferred start, the submitted
    hint and both actions; the accepted card drops the Edit button."""
    submitted = _fake_request_row(
        'submitted',
        number=1,
        name='Open Cup',
        preferred_start_time=_fake_dt('2026-10-03', '14:00'),
    )
    accepted = _fake_request_row(
        'accepted',
        number=2,
        name='Accepted Cup',
        decided_at=_fake_dt('2026-09-27', '10:12'),
    )

    html = _render_my_requests(
        bote_env,
        requests=[submitted, accepted],
        status_counts={'submitted': 1, 'accepted': 1},
        open_requests=[submitted, accepted],
        waiting_days_by_request_id={submitted.id: 3},
    )

    assert 'class="card-t"' in html
    assert 'Wish start' in html
    assert '<b>[dd.MM. HH:mm] 2026-10-03</b>' in html
    assert 'Preferred start' not in html
    assert 'Waiting for the orga for 3 days.' in html
    assert 'Accepted on 2026-09-27' in html
    assert 'The orga is setting up the tournament.' in html

    assert html.count('class="button btn-sm"') == 1
    assert html.count('class="lnk"') == 2
    assert 'update_request_form' in html


def test_bote_my_requests_card_zero_days_says_submitted_today(bote_env):
    """A4 (fix cycle 1, Issue 8): a request submitted today (0
    waiting days) says "Submitted today. Waiting for the orga.", not
    the `ngettext` plural "Waiting for the orga for 0 days."."""
    submitted = _fake_request_row(
        'submitted',
        number=1,
        name='Fresh Cup',
        preferred_start_time=_fake_dt('2026-10-03', '14:00'),
    )

    html = _render_my_requests(
        bote_env,
        requests=[submitted],
        status_counts={'submitted': 1},
        open_requests=[submitted],
        waiting_days_by_request_id={submitted.id: 0},
    )

    assert 'Submitted today. Waiting for the orga.' in html
    assert '0 days' not in html
    assert '0 day.' not in html


def test_bote_my_requests_tracker_marks_current_step(bote_env):
    """AC1: submitted marks step 1 as current (step 0 done, step 2
    untouched); accepted marks step 2 as current (steps 0-1 done)."""
    submitted = _fake_request_row('submitted', number=1, name='Open Cup')
    accepted = _fake_request_row('accepted', number=2, name='Accepted Cup')

    html = _render_my_requests(
        bote_env,
        requests=[submitted, accepted],
        status_counts={'submitted': 1, 'accepted': 1},
        open_requests=[submitted, accepted],
    )

    submitted_card = html[html.index('Open Cup') : html.index('Accepted Cup')]
    accepted_card = html[html.index('Accepted Cup') :]

    # submitted: "Submitted" done, "Decided" current, "In program" bare.
    assert (
        '<li class="done"><i></i><span>Submitted</span></li>' in submitted_card
    )
    assert (
        '<li class="next" aria-current="step"><i></i><span>Decided</span></li>'
        in submitted_card
    )
    assert '<li class=""><i></i><span>In program</span></li>' in submitted_card

    # accepted: "Submitted"/"Decided" done, "In program" current.
    assert (
        '<li class="done"><i></i><span>Submitted</span></li>' in accepted_card
    )
    assert '<li class="done"><i></i><span>Decided</span></li>' in accepted_card
    assert (
        '<li class="next" aria-current="step"><i></i><span>In program</span></li>'
        in accepted_card
    )


def test_bote_my_requests_win_card_shows_orga_and_signups(bote_env):
    """AC2: the win card shows the tournament start, the signup
    count when a limit exists, and the "you are orga" line when the
    caller is an orga of that tournament."""
    created = _fake_request_row(
        'tournament_created',
        number=3,
        name='Live Cup',
        created_tournament_id='t-1',
    )
    tournament = _fake_tournament(
        max_players=64, start_time=_fake_dt('2026-10-02', '18:00')
    )

    html = _render_my_requests(
        bote_env,
        requests=[created],
        live_requests=[created],
        tournaments_by_request_id={created.id: tournament},
        signup_counts={tournament.id: (23, 64)},
        orga_tournament_ids={tournament.id},
    )

    assert 'class="win-h"' in html
    assert 'Your tournament' in html
    assert '23 of 64' in html
    assert 'Orga of this tournament' in html
    assert 'class="button btn-ok btn-full"' in html
    assert 'To the tournament' in html


def test_bote_my_requests_win_card_hides_signup_and_orga_lines_when_absent(
    bote_env,
):
    """Negative case: no limit and not an orga -- neither line renders."""
    created = _fake_request_row(
        'tournament_created',
        number=3,
        name='Live Cup',
        created_tournament_id='t-1',
    )
    tournament = _fake_tournament()

    html = _render_my_requests(
        bote_env,
        requests=[created],
        live_requests=[created],
        tournaments_by_request_id={created.id: tournament},
        participant_counts={},
        orga_tournament_ids=set(),
    )

    assert 'Signed up' not in html
    assert 'Orga of this tournament' not in html


def test_bote_my_requests_archive_shows_meta_and_reason_lead(bote_env):
    """The archive items show game and date, withdrawn or rejected state."""
    rejected = _fake_request_row(
        'rejected',
        number=4,
        name='Rejected Cup',
        rejection_reason='Not enough interest.',
        created_at=_fake_dt('2026-09-12', '09:00'),
    )
    withdrawn = _fake_request_row(
        'withdrawn',
        number=5,
        name='Withdrawn Cup',
        updated_at=_fake_dt('2026-09-11', '09:00'),
    )

    html = _render_my_requests(
        bote_env,
        requests=[rejected, withdrawn],
        status_counts={'rejected': 1, 'withdrawn': 1},
        archived_requests=[rejected, withdrawn],
        reason_leads_by_request_id={
            rejected.id: ('Not enough interest.', False)
        },
    )

    assert 'class="dl-list"' in html
    assert 'Test Game' in html
    assert 'submitted 2026-09-12' in html
    assert 'withdrawn by you on 2026-09-11' in html
    assert '<span class="why-sm">„Not enough interest.“</span>' in html
    assert html.count('class="why-sm"') == 1
    assert 'Reason from the orga' not in html
    assert '<q>' not in html


def test_bote_my_requests_renders_empty_state(bote_env):
    html = _render_my_requests(bote_env)

    assert 'Propose a tournament' in html
    assert 'class="facts cnt"' not in html
    assert 'class="cards two-c"' not in html
    # no header CTA while there is nothing to manage yet -- only the
    # empty-state box's own button.
    assert html.count('btn-pri') == 1


def test_bote_my_requests_empty_state_is_the_drafts_empty_box(bote_env):
    """The empty state is a heading, an italic line and a CTA."""
    html = _render_my_requests(bote_env)

    box = html[html.index('<div class="empty-box">') :]
    box = box[: box.index('</div>') + len('</div>')]

    assert '<h3>No requests yet.</h3>' in box
    assert (
        '<p>Got an idea for the program? Propose a tournament, the orga '
        'will get back to you.</p>'
    ) in box
    assert (
        '<a class="button btn-pri" href="/propose_form">'
        'Propose a tournament</a>'
    ) in box
    assert '+' not in box
    assert not re.search(r'class="[^"]*\bfs\b', html)
    assert 'You have not proposed' not in html
    assert '<header class="head">' in html
    assert 'class="tags"' not in html


def test_bote_my_requests_header_sub_names_the_party(bote_env):
    html = _render_my_requests(bote_env)

    assert '<div class="sub">Party total-verplant 36</div>' in html


_SECTION_HEAD = re.compile(
    r'<div class="(sh[^"]*)"><h2>([^<]*)</h2><span>([^<]*)</span></div>'
)


def test_bote_my_requests_section_heads_carry_counts(bote_env):
    """Every section head is a bare `.sh` counting its entries."""
    open_a = _fake_request_row('submitted', number=1, name='A Cup')
    open_b = _fake_request_row('accepted', number=2, name='B Cup')
    rejected = _fake_request_row('rejected', number=3, name='C Cup')
    created = _fake_request_row(
        'tournament_created',
        number=4,
        name='D Cup',
        created_tournament_id='t-1',
    )

    html = _render_my_requests(
        bote_env,
        requests=[open_a, open_b, rejected, created],
        status_counts={'submitted': 1, 'accepted': 1, 'rejected': 1},
        open_requests=[open_a, open_b],
        archived_requests=[rejected],
        live_requests=[created],
        tournaments_by_request_id={created.id: _fake_tournament()},
    )

    assert _SECTION_HEAD.findall(html) == [
        ('sh sh-top', 'In progress', '2'),
        ('sh', 'Archive', '1'),
        ('sh sh-top', 'In the program', '1'),
    ]
    assert not re.search(r'class="[^"]*\b(fs|desk)\b', html)
    assert '<aside' not in html


def test_bote_my_requests_archive_head_gets_the_top_gap_without_open_cards(
    bote_env,
):
    """The archive head is the first head and needs the top margin."""
    rejected = _fake_request_row('rejected', number=3, name='C Cup')

    html = _render_my_requests(
        bote_env,
        requests=[rejected],
        status_counts={'rejected': 1},
        archived_requests=[rejected],
    )

    assert _SECTION_HEAD.findall(html) == [('sh sh-top', 'Archive', '1')]


def test_bote_my_requests_reason_link_only_when_more_text_follows(bote_env):
    """The archive row quotes the first sentence of the reason."""
    long_reason = _fake_request_row(
        'rejected', number=1, name='Long Cup', rejection_reason='One. Two.'
    )
    short_reason = _fake_request_row(
        'rejected', number=2, name='Short Cup', rejection_reason='Only one.'
    )

    html = _render_my_requests(
        bote_env,
        requests=[long_reason, short_reason],
        status_counts={'rejected': 2},
        archived_requests=[long_reason, short_reason],
        reason_leads_by_request_id={
            long_reason.id: ('One.', True),
            short_reason.id: ('Only one.', False),
        },
    )

    assert (
        '<span class="why-sm">„One.“ '
        '<a href="/update_request_form">Full reason</a></span>'
    ) in html
    assert '<span class="why-sm">„Only one.“</span>' in html
    assert html.count('Full reason') == 1


def test_bote_my_requests_rejected_row_without_a_lead_has_no_reason(bote_env):
    """A rejected request that has no reason (legacy row) shows none."""
    rejected = _fake_request_row(
        'rejected', number=1, name='Legacy Cup', rejection_reason=None
    )

    html = _render_my_requests(
        bote_env,
        requests=[rejected],
        status_counts={'rejected': 1},
        archived_requests=[rejected],
    )

    assert 'Legacy Cup' in html
    assert 'why-sm' not in html


def test_bote_my_requests_reason_lead_is_escaped(bote_env):
    rejected = _fake_request_row('rejected', number=1, name='Cup')

    html = _render_my_requests(
        bote_env,
        requests=[rejected],
        status_counts={'rejected': 1},
        archived_requests=[rejected],
        reason_leads_by_request_id={
            rejected.id: ('<script>alert(1)</script>', False)
        },
    )

    assert '<script>' not in html
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in html


def test_bote_my_requests_win_card_start_asks_for_the_weekday_pattern(
    bote_env,
):
    """The wish start reads "Fr 02.10. · 18:00"."""
    created = _fake_request_row(
        'tournament_created',
        number=3,
        name='Live Cup',
        created_tournament_id='t-1',
    )
    with_start = _fake_tournament(start_time=_fake_dt('2026-10-02', '18:00'))

    html = html_unescape(
        _render_my_requests(
            bote_env,
            requests=[created],
            live_requests=[created],
            tournaments_by_request_id={created.id: with_start},
        )
    )

    assert "<dd>[ccc dd.MM. '·' HH:mm] 2026-10-02</dd>" in html

    no_start = html_unescape(
        _render_my_requests(
            bote_env,
            requests=[created],
            live_requests=[created],
            tournaments_by_request_id={created.id: _fake_tournament()},
        )
    )

    assert '<dd>—</dd>' in no_start
    assert 'ccc' not in no_start


def _signup_counts_for(tournament, participant_counts, team_counts):
    from byceps.services.lan_tournament.blueprints.site.views import (
        _signup_counts,
    )

    return _signup_counts([tournament], participant_counts, team_counts)


@pytest.mark.parametrize(
    ('tournament', 'expected'),
    [
        (
            _fake_tournament(contestant_type=ContestantType.TEAM, max_teams=8),
            '3 of 8',
        ),
        (
            _fake_tournament(
                contestant_type=ContestantType.SOLO,
                max_players=16,
                max_teams=8,
            ),
            '5 of 16',
        ),
    ],
)
def test_bote_my_requests_win_card_counts_by_contestant_type(
    bote_env, tournament, expected
):
    """The win card counts teams or players by the contestant type."""
    created = _fake_request_row(
        'tournament_created',
        number=3,
        name='Live Cup',
        created_tournament_id='t-1',
    )

    html = _render_my_requests(
        bote_env,
        requests=[created],
        live_requests=[created],
        tournaments_by_request_id={created.id: tournament},
        signup_counts=_signup_counts_for(
            tournament, {tournament.id: 5}, {tournament.id: 3}
        ),
    )

    assert '<dt>Signed up</dt>' in html
    assert expected in html
    assert '0 of' not in html


def test_bote_my_requests_win_card_without_a_limit_hides_the_signup_row(
    bote_env,
):
    """A tournament without a limit shows no "Signed up" row."""
    created = _fake_request_row(
        'tournament_created',
        number=3,
        name='Live Cup',
        created_tournament_id='t-1',
    )
    tournament = _fake_tournament(
        contestant_type=ContestantType.TEAM, max_players=32
    )

    html = _render_my_requests(
        bote_env,
        requests=[created],
        live_requests=[created],
        tournaments_by_request_id={created.id: tournament},
        signup_counts=_signup_counts_for(
            tournament, {tournament.id: 20}, {tournament.id: 3}
        ),
    )

    assert 'Signed up' not in html
    assert 'None' not in html


def test_bote_my_requests_win_card_without_a_count_shows_zero_of_the_limit(
    bote_env,
):
    """A tournament nobody has joined yet still shows "0 of N"."""
    created = _fake_request_row(
        'tournament_created',
        number=3,
        name='Live Cup',
        created_tournament_id='t-1',
    )

    for tournament, expected in (
        (
            _fake_tournament(contestant_type=ContestantType.TEAM, max_teams=8),
            '0 of 8',
        ),
        (
            _fake_tournament(
                contestant_type=ContestantType.SOLO, max_players=16
            ),
            '0 of 16',
        ),
    ):
        html = _render_my_requests(
            bote_env,
            requests=[created],
            live_requests=[created],
            tournaments_by_request_id={created.id: tournament},
            signup_counts=_signup_counts_for(tournament, {}, {}),
        )

        assert expected in html


def test_bote_my_requests_short_dates_render_the_drafts_forms_with_babel():
    """The real CLDR patterns render the draft's date formats."""
    from babel import dates

    env = _make_env({'my_requests': _snippet(_BOTE_MY_REQUESTS_TEMPLATE)})
    env.filters['datetimeformat'] = lambda dt, fmt: dates.format_datetime(
        dt, fmt, locale='de'
    )
    env.filters['dateformat'] = lambda dt, fmt='medium': dates.format_date(
        dt, fmt, locale='de'
    )
    submitted = _fake_request_row(
        'submitted',
        number=1,
        name='Open Cup',
        preferred_start_time=datetime(2026, 10, 3, 14, 0),
    )
    accepted = _fake_request_row(
        'accepted',
        number=2,
        name='Accepted Cup',
        preferred_start_time=datetime(2026, 10, 3, 21, 0),
        decided_at=datetime(2026, 9, 27, 10, 12),
    )
    rejected = _fake_request_row(
        'rejected',
        number=3,
        name='Rejected Cup',
        created_at=datetime(2026, 9, 12, 9, 0),
    )
    withdrawn = _fake_request_row(
        'withdrawn',
        number=4,
        name='Withdrawn Cup',
        updated_at=datetime(2026, 9, 11, 9, 0),
    )
    created = _fake_request_row(
        'tournament_created',
        number=5,
        name='Live Cup',
        created_tournament_id='t-1',
    )
    tournament = _fake_tournament(
        max_players=64, start_time=datetime(2026, 10, 2, 18, 0)
    )

    html = html_unescape(
        _render_my_requests(
            env,
            requests=[submitted, accepted, rejected, withdrawn, created],
            open_requests=[submitted, accepted],
            archived_requests=[rejected, withdrawn],
            live_requests=[created],
            tournaments_by_request_id={created.id: tournament},
            signup_counts={tournament.id: (23, 64)},
        )
    )

    assert '<b>03.10. 14:00</b>' in html
    # The msgid ends in a full stop, so the date is `dd.MM`.
    assert 'Accepted on 27.09. The orga is setting up the tournament.' in html
    assert '..' not in html
    assert 'submitted 12.09.</span>' in html
    assert 'withdrawn by you on 11.09.</span>' in html
    assert '<dd>Fr 02.10. · 18:00</dd>' in html
    assert '%(' not in html


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


_DRAFT_TOKEN_HEX = ['#7a5410', '#1f4470', '#d19a4c', '#8fa9d2']


def test_bote_style_partial_has_no_hardcoded_hex_colours():
    """Only the `--ochre` and `--blue` tokens hold hex colours."""
    src = _BOTE_STYLE_PARTIAL.read_text()

    hex_colours = re.findall(r'#[0-9a-fA-F]{3,8}', src)
    assert hex_colours == _DRAFT_TOKEN_HEX, hex_colours


def test_bote_style_partial_parses_as_a_jinja_template():
    """The partial is `{% include %}`d into real pages; a syntax error
    in it would 500 every page that includes it."""
    Environment(autoescape=True).parse(_BOTE_STYLE_PARTIAL.read_text())


def test_bote_partial_css_comments_have_no_process_refs():
    """A3 (fix cycle 1, Issues 7/8): the shipped CSS comments must not
    leak internal process references (a plan/issue number, a section
    mark, a test-suite name) into a page the browser fetches. Design
    vocabulary ("paper ground", "slip", "desk", "carbon", ...) is
    fine."""
    src = _BOTE_STYLE_PARTIAL.read_text()
    comments = re.findall(r'/\*.*?\*/', src, flags=re.DOTALL)

    assert comments, 'expected at least one CSS comment to check'

    process_ref = re.compile(r'Issue \d|§|Patch|writeup|test_')
    offenders = [c for c in comments if process_ref.search(c)]

    assert not offenders, offenders


# ------------------------------------------------------------------ #
# F2 (fix cycle 1, Issue 7) -- input rules must beat the theme's
# ------------------------------------------------------------------ #


_THEME_CSS_PATH = pathlib.Path(
    'sites/totalverplant-36/static/style/totalverplant-36.css'
)


def _strip_css_comments(css: str) -> str:
    return re.sub(r'/\*.*?\*/', ' ', css, flags=re.DOTALL)


def _css_specificity(selector: str) -> tuple[int, int, int]:
    """Hand-rolled (a, b, c) specificity for a single (comma-free)
    CSS selector: a = ID selectors, b = classes/attribute selectors/
    pseudo-classes, c = type selectors/pseudo-elements.

    `:not(x)` contributes nothing itself; only `x`'s own selectors
    count (per the CSS spec) -- handled below by unwrapping the
    `:not(...)` shell before counting anything else.
    """
    prev = None
    while prev != selector:
        prev = selector
        selector = re.sub(r':not\(([^()]*)\)', r' \1 ', selector)

    ids = len(re.findall(r'#[\w-]+', selector))
    classes = len(re.findall(r'\.[\w-]+', selector))
    attrs = len(re.findall(r'\[[^\]]*\]', selector))

    _LEGACY_PSEUDO_ELEMENTS = {'before', 'after', 'first-line', 'first-letter'}
    single_colon_names = re.findall(r'(?<!:):([a-zA-Z-]+)', selector)
    pseudo_classes = sum(
        1 for n in single_colon_names if n not in _LEGACY_PSEUDO_ELEMENTS
    )
    pseudo_elements = sum(
        1 for n in single_colon_names if n in _LEGACY_PSEUDO_ELEMENTS
    )
    pseudo_elements += len(re.findall(r'::[a-zA-Z-]+', selector))

    b = classes + attrs + pseudo_classes

    remainder = re.sub(
        r'\.[\w-]+|#[\w-]+|\[[^\]]*\]|::?[a-zA-Z-]+', ' ', selector
    )
    types = len(re.findall(r'[a-zA-Z][\w-]*', remainder))
    c = types + pseudo_elements

    return (ids, b, c)


def _extract_selector_for_rule_with(src: str, declaration_marker: str) -> str:
    """Return the (whitespace-trimmed) selector of the single,
    non-nested CSS rule in `src` whose declaration block contains
    `declaration_marker`. `src` must already have its comments
    stripped, so a preceding comment's prose can't leak into the
    captured selector."""
    match = re.search(
        r'([^{}]+)\{[^{}]*' + re.escape(declaration_marker) + r'[^{}]*\}',
        src,
    )
    assert match, f'no rule found for marker {declaration_marker!r}'
    return match.group(1).strip()


def test_css_specificity_calculator_matches_the_documented_theme_value():
    """Sanity-check `_css_specificity` itself against the theme's own
    documented value (`totalverplant-36.css:99`, "(0,3,1)" is exactly
    what this bug report measured in Chromium) before trusting it to
    judge the bote partial's rules below."""
    theme_src = _strip_css_comments(_THEME_CSS_PATH.read_text())
    match = re.search(
        r"input\.form-control:not\(\[type='checkbox'\]\):not\(\[type='radio'\]\)",
        theme_src,
    )
    assert match, 'theme input.form-control rule not found'

    assert _css_specificity(match.group(0)) == (0, 3, 1)


def test_bote_input_rules_beat_theme_input_specificity():
    """F2: `.request-page .form-control` and `.request-page .invalid
    .form-control` used to lose the specificity war against the
    theme's `input.form-control:not(...):not(...)` rule (0,3,1) --
    Chromium kept the theme's ink border/--paper/18px on `<input>`
    elements. Both rules must now clear that bar."""
    src = _strip_css_comments(_BOTE_STYLE_PARTIAL.read_text())

    base_selector = _extract_selector_for_rule_with(src, 'height: 48px')
    invalid_selector = _extract_selector_for_rule_with(
        src, 'box-shadow: inset 0 0 0 1.5px var(--red)'
    )

    theme_specificity = (0, 3, 1)
    base_specificity = _css_specificity(base_selector)
    invalid_specificity = _css_specificity(invalid_selector)

    assert base_specificity > theme_specificity, (
        base_selector,
        base_specificity,
    )
    assert invalid_specificity > theme_specificity, (
        invalid_selector,
        invalid_specificity,
    )
    # No `!important` anywhere in the partial (briefing: never use it).
    assert '!important' not in src


def test_bote_over_and_textarea_rules_beat_the_base_input_rule():
    """B2 (workspace-pv3b.22): the (0,4,0) base `.form-control` rule
    from fix cycle 1 overrode `.over`'s red border/text and made the
    textarea `min-height` dead. Both must now clear that (0,4,0) bar
    -- via ancestor specificity, never `!important` or a hex color."""
    src = _strip_css_comments(_BOTE_STYLE_PARTIAL.read_text())

    base_selector = _extract_selector_for_rule_with(src, 'height: 48px')
    over_selector = _extract_selector_for_rule_with(
        src, 'border-color: var(--red); color: var(--red);'
    )
    textarea_selector = _extract_selector_for_rule_with(
        src, 'min-height: 104px'
    )

    base_specificity = _css_specificity(base_selector)
    over_specificity = _css_specificity(over_selector)
    textarea_specificity = _css_specificity(textarea_selector)

    assert over_specificity > base_specificity, (
        over_selector,
        over_specificity,
        base_specificity,
    )
    assert textarea_specificity > base_specificity, (
        textarea_selector,
        textarea_specificity,
        base_specificity,
    )
    assert '!important' not in src
    # `--ochre` and `--blue` are the only hex exceptions.
    non_token_hex = [
        h
        for h in re.findall(r'#[0-9a-fA-F]{3,8}\b', src)
        if h not in _DRAFT_TOKEN_HEX
    ]
    assert not non_token_hex, non_token_hex


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


def _make_tournament_stub(tournament_id, tournament_status):
    """Build a minimal `created_tournament` stub for the frozen context.

    Exposes only what `propose_form.html` reads: `id` and
    `tournament_status` (with `.name`).
    """
    return SimpleNamespace(
        id=tournament_id, tournament_status=tournament_status
    )


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

    assert 'class="stamp st-tournament_deleted"' in html
    assert 'The tournament created from your request has been deleted.' in html
    assert 'class="stamp st-tournament_created"' not in html


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

    assert 'class="stamp st-tournament_created"' in html
    assert 'has been deleted' not in html


def test_bote_propose_form_frozen_mode_has_no_history_desk(bote_env):
    """The frozen frame has no history desk, even with log entries."""
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

    assert 'class="desk"' not in html
    assert 'desk-h' not in html
    assert 'Spring Cup' not in html


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
    """AC3: the negative case is unchanged -- the win card renders
    normally (Issue 8's win card no longer carries a `.st` tag at
    all, see `test_bote_my_requests_win_card_shows_orga_and_signups`)
    and the deleted-tournament note never appears."""
    live_request = _make_real_request(
        status=TournamentRequestStatus.tournament_created,
        created_tournament_id=TournamentID(generate_uuid()),
    )
    tournament = _fake_tournament()

    html = _render_my_requests(
        bote_env,
        requests=[live_request],
        live_requests=[live_request],
        tournaments_by_request_id={live_request.id: tournament},
        participant_counts={},
    )

    assert 'class="win-h"' in html
    assert 'Live Tournament' in html
    assert 'has been deleted' not in html
    assert 'class="st st-tournament_deleted"' not in html


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


def test_bote_propose_form_frozen_mode_shows_translated_format_and_mode_labels(
    bote_env,
):
    """The frozen summary renders the passed labels, not enum names."""
    tournament_request = _make_tournament_request(number=3, status='rejected')
    assert (
        tournament_request.elimination_mode
        is EliminationMode.SINGLE_ELIMINATION
    )

    html = _render_propose_form(
        bote_env,
        mode='frozen',
        form=None,
        tournament_request=tournament_request,
        party_capacity=None,
        history=[],
        format_label='Eins gegen eins (marker)',
        request_mode_label='Einfaches K.-o. (marker)',
    )

    assert 'Eins gegen eins (marker) · Einfaches K.-o. (marker)' in html
    assert 'Single Elimination' not in html
    assert 'SINGLE_ELIMINATION' not in html
    assert 'ONE_V_ONE' not in html


def test_bote_my_requests_draft_tournament_has_no_link_and_shows_label(
    bote_env,
):
    """h: the bote override must never link a DRAFT tournament either."""
    created = _fake_request_row(
        'tournament_created',
        number=3,
        name='Live Cup',
        created_tournament_id='t-1',
    )
    tournament = _fake_tournament(name='Live Cup')

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
        'tournament_created',
        number=4,
        name='Open Cup',
        created_tournament_id='t-2',
    )
    tournament = _fake_tournament('t-2', name='Open Cup')

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


def _edited_entry(
    changed_fields,
    *,
    previous_values=None,
    new_values=None,
    by='proposer',
    occurred_at=None,
):
    data = {'changed_fields': changed_fields, 'by': by}
    if previous_values is not None:
        data['previous_values'] = previous_values
    if new_values is not None:
        data['new_values'] = new_values
    return SimpleNamespace(
        event_type='tournament-request-edited',
        occurred_at=occurred_at or _fake_dt(),
        data=data,
    )


def _flat(html: str) -> str:
    """Collapse whitespace, the way a browser renders running text."""
    return ' '.join(html.split())


def _render_edit(bote_env, *, form=None, party_capacity=None, **ctx):
    ctx.setdefault('history', [])
    return _render_propose_form(
        bote_env,
        mode='edit',
        form=form or _make_form(),
        tournament_request=_make_tournament_request(status='submitted'),
        party_capacity=party_capacity,
        **ctx,
    )


def _limit_block(html: str) -> str:
    """The `.form-control-block` that holds the participant limit input."""
    marker = html.index('id="participant_limit"')
    start = html.rindex('<div class="form-control-block', 0, marker)
    end = html.index('\n          </div>', marker)
    return html[start:end]


def test_bote_crumbs_are_all_links_and_the_third_links_the_request(bote_env):
    create_html = _render_propose_form(
        bote_env, mode='create', form=_make_form()
    )
    edit_html = _render_edit(bote_env)
    frozen_html = _render_propose_form(
        bote_env,
        mode='frozen',
        form=None,
        tournament_request=_make_tournament_request(status='accepted'),
    )

    for html in (create_html, edit_html, frozen_html):
        assert '<a href="/index">Tournaments</a><span>»</span>' in html
        assert '<a href="/my_requests">My requests</a><span>»</span>' in html
    assert 'href="/update_request_form"' not in create_html
    for html in (edit_html, frozen_html):
        assert (
            '<a href="/update_request_form">Test Cup</a><span>»</span>' in html
        )


def test_bote_ticket_gate_uses_the_warn_header_bar_not_a_heading(bote_env):
    html = _render_propose_form(
        bote_env, mode='create', form=_make_form(), has_ticket=False
    )

    assert '<div class="warn-h">Ticket required</div>' in html
    assert '<h2>Ticket required' not in html
    assert '<h2>' not in html.split('<form')[0].split('class="warn"')[1]


def test_bote_error_summary_renders_the_forms_field_phrase_triples(bote_env):
    form = _make_form(errors={'name': ['Required field']})
    form.name.errors = ['Required field']
    form.error_summary = lambda: [('name', 'Name', 'is missing')]

    html = _render_propose_form(bote_env, mode='create', form=form)

    assert '1 entry to check' in html
    assert '<li><a href="#name">Name</a> is missing</li>' in html
    assert 'Required field' in html


def test_bote_game_format_radios_carry_the_format_label_for_the_hint(
    bote_env,
):
    html = _render_propose_form(bote_env, mode='create', form=_make_form())

    for option in _GAME_FORMAT_OPTIONS:
        assert (
            f'value="{option["value"]}" data-format-label="{option["label"]}"'
            in html
        )


def _mode_hint(html: str) -> re.Match:
    match = re.search(
        r'<p class="hn form-caption" data-mode-hint'
        r' data-hint-template="([^"]*)" data-hint-default="([^"]*)">'
        r'([^<]*)</p>',
        html,
    )
    assert match, html
    return match


def test_bote_mode_hint_names_the_selected_format_and_carries_templates(
    bote_env,
):
    html = _render_propose_form(bote_env, mode='create', form=_make_form())

    assert html.count('data-mode-hint') == 1
    template, default, text = _mode_hint(html).groups()
    assert html_unescape(template) == (
        'Struck-through modes do not fit “{format}”.'
    )
    assert html_unescape(default) == (
        'Struck-through modes do not fit the selected game format.'
    )
    assert text == 'Struck-through modes do not fit “One on one”.'
    assert re.search(
        r'id="elimination_mode">.*?</div>\s*<p class="hn form-caption"',
        html,
        flags=re.DOTALL,
    )


def test_bote_mode_hint_falls_back_to_the_default_without_a_format(bote_env):
    form = _make_form()
    form.game_format.data = None

    html = _render_propose_form(bote_env, mode='create', form=form)

    text = _mode_hint(html).group(3)
    assert text == 'Struck-through modes do not fit the selected game format.'


def test_bote_mode_hint_attributes_round_trip_markup_msgstrs():
    """Quotes in the hint's template and default survive `forceescape`."""
    template_msgstr = 'Don\'t "fit" „%(format)s“ &'
    default_msgstr = 'Plain "default" &'
    env = _make_env({'propose_form': _snippet(_BOTE_PROPOSE_TEMPLATE)})
    env.globals['_'] = _markup_gettext(
        {
            'Struck-through modes do not fit “%(format)s”.': template_msgstr,
            'Struck-through modes do not fit the selected game format.': (
                default_msgstr
            ),
        }
    )

    html = _render_propose_form(env, mode='create', form=_make_form())

    assert template_msgstr not in html
    assert default_msgstr not in html
    template, default, _text = _mode_hint(html).groups()
    assert html_unescape(template) == 'Don\'t "fit" „{format}“ &'
    assert html_unescape(default) == default_msgstr


def test_bote_limit_field_carries_three_caption_templates_and_the_caption(
    bote_env,
):
    form = _make_form()
    form.team_size.data = 1

    html = _render_propose_form(
        bote_env, mode='create', form=form, party_capacity=240
    )
    block = _limit_block(html)

    assert (
        'data-caption-template="2 to {max} · 240 seats at the party"' in block
    )
    assert (
        'data-caption-template-teams="2 to {max} teams · 240 seats ÷ {size}"'
        in block
    )
    assert (
        'data-caption-template-over="Team size too large for 240 seats"'
        in block
    )
    assert (
        '<div class="form-caption" data-limit-caption>'
        '2 to 240 · 240 seats at the party</div>'
    ) in block
    assert 'class="cap"' not in html
    assert html.count('data-limit-caption') == 1


@pytest.mark.parametrize(
    ('team_size', 'caption'),
    [
        (4, '2 to 60 teams · 240 seats ÷ 4'),
        (300, 'Team size too large for 240 seats'),
    ],
)
def test_bote_limit_caption_starts_in_the_script_s_wording(
    bote_env, team_size, caption
):
    form = _make_form()
    form.team_size.data = team_size

    html = _render_propose_form(
        bote_env, mode='create', form=form, party_capacity=240
    )

    assert f'data-limit-caption>{caption}</div>' in _limit_block(html)


def test_bote_limit_caption_is_capped_by_the_server_side_maximum(bote_env):
    form = _make_form()
    form.team_size.data = 1

    html = _render_propose_form(
        bote_env, mode='create', form=form, party_capacity=5000
    )

    assert (
        f'data-limit-caption>2 to {MAX_PARTICIPANT_LIMIT} · 5000 seats'
        in _limit_block(html)
    )


def test_bote_limit_caption_hook_stays_but_is_hidden_without_capacity(
    bote_env,
):
    html = _render_propose_form(
        bote_env, mode='create', form=_make_form(), party_capacity=None
    )

    assert '<div class="form-caption" data-limit-caption hidden></div>' in html
    assert 'data-caption-template' not in html


def test_bote_edit_without_latest_changes_shows_no_markers(bote_env):
    for html in (
        _render_propose_form(bote_env, mode='create', form=_make_form()),
        _render_edit(bote_env, latest_changes={}),
    ):
        assert 'chg-t' not in html
        assert 'form-control-block chg' not in html
        assert 'Before:' not in html
        assert 'data-before' not in html


def test_bote_edit_marks_changed_fields_with_chip_and_before_line(bote_env):
    html = _render_edit(
        bote_env,
        latest_changes={
            'name': 'Old Cup',
            'special_rules': '—',
            'game_format': 'Highscore',
            'elimination_mode': 'No knockout',
        },
    )

    assert html.count('form-control-block chg') == 4
    assert html.count('<i class="opt-chip chg-t">changed</i>') == 4
    assert (
        '<label class="form-label" for="name">Name'
        ' <i class="opt-chip chg-t">changed</i></label>'
    ) in html
    assert (
        'Special rules <i class="opt-chip">optional</i>'
        ' <i class="opt-chip chg-t">changed</i></label>'
    ) in html
    assert '<div class="form-caption">Before: Old Cup</div>' in html
    assert '<div class="form-caption">Before: —</div>' in html
    assert '<div class="form-caption">Before: Highscore</div>' in html
    assert '<div class="form-caption">Before: No knockout</div>' in html
    assert 'Before: Old Cup ·' not in html
    assert 'for="game">Game</label>' in html


def test_bote_edit_before_value_is_escaped(bote_env):
    html = _render_edit(
        bote_env, latest_changes={'name': '<script>alert(1)</script>'}
    )

    assert '<script>alert(1)' not in html
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in html


def test_bote_changed_limit_keeps_chip_beside_the_label_and_before_prefix(
    bote_env,
):
    """The chip and the "Before" prefix stay out of the script's reach."""
    form = _make_form()
    form.team_size.data = 1

    html = _render_edit(
        bote_env,
        form=form,
        party_capacity=240,
        latest_changes={'participant_limit': '32'},
    )
    block = _limit_block(html)

    assert 'form-control-block chg' in block
    assert (
        '<div class="lbl-row"><label class="form-label"'
        ' for="participant_limit">Participant limit</label>'
        ' <i class="opt-chip chg-t">changed</i></div>'
    ) in block
    label = re.search(r'<label[^>]*>[^<]*(<[^>]*>)*[^<]*</label>', block)
    assert label and 'chg-t' not in label.group(0)
    assert (
        '<div class="form-caption" data-limit-caption'
        ' data-before="Before: 32 · ">2 to 240 · 240 seats at the party</div>'
    ) in block
    assert block.count('Before:') == 1


def test_bote_changed_limit_without_capacity_shows_its_own_before_line(
    bote_env,
):
    html = _render_edit(
        bote_env,
        party_capacity=None,
        latest_changes={'participant_limit': '32'},
    )
    block = _limit_block(html)

    assert 'data-before' not in block
    assert '<div class="form-caption" data-limit-caption hidden></div>' in block
    assert '<div class="form-caption">Before: 32</div>' in block


def test_bote_history_desk_has_the_dark_header_bar(bote_env):
    history = [
        _make_history_entry('tournament-request-submitted'),
        _edited_entry(['special_rules']),
    ]

    html = _render_edit(bote_env, history=history)

    assert '<aside class="desk">' in html
    assert '<div class="desk-h">History</div>' in html
    assert '<div class="desk-b">' in html
    assert '<ol class="tl">' in html
    assert '<p class="hint">Every change you save appears here' in html
    assert 'class="sh"><h2>History' not in html
    assert (
        html.index('</form>')
        < html.index('class="danger-zone"')
        < html.index('class="desk"')
    )


def test_bote_history_entries_use_the_short_date_pattern(bote_env):
    html = _render_edit(
        bote_env,
        history=[_make_history_entry('tournament-request-submitted')],
    )

    assert '<span class="lbl">[dd.MM. HH:mm] ' in html


def test_bote_history_shows_an_arrow_for_a_changed_limit(bote_env):
    history = [
        _make_history_entry('tournament-request-submitted'),
        _edited_entry(
            ['description', 'participant_limit', 'team_size'],
            previous_values={
                'description': 'old',
                'participant_limit': 32,
                'team_size': 1,
            },
            new_values={'participant_limit': 40, 'team_size': 2},
        ),
    ]

    html = _flat(_render_edit(bote_env, history=history))

    assert (
        'Edited by you · Short description, Participant limit 32 → 40,'
        ' Team size 1 → 2'
    ) in html


def test_bote_history_edit_without_previous_values_has_no_markers(bote_env):
    history = [
        _make_history_entry('tournament-request-submitted'),
        _edited_entry(['participant_limit', 'name']),
    ]

    html = _flat(_render_edit(bote_env, history=history))

    assert 'Edited by you · Participant limit, Name' in html
    assert '→' not in html


def test_bote_history_attributes_admin_edits_to_the_orga(bote_env):
    history = [
        _make_history_entry('tournament-request-submitted'),
        _edited_entry(['elimination_mode'], by='admin'),
    ]

    html = _flat(_render_edit(bote_env, history=history))

    assert 'Edited by the orga · Tournament mode' in html
    assert 'Edited by you' not in html
    assert 'Elimination mode' not in html.split('class="desk"')[1]


def test_bote_frozen_summary_is_six_rows_in_a_sup_list(bote_env):
    tournament_request = _make_tournament_request(number=139, status='accepted')

    html = _render_propose_form(
        bote_env,
        mode='frozen',
        form=None,
        tournament_request=tournament_request,
        format_label='Highscore',
        request_mode_label='No knockout',
    )

    assert html.count('<dl class="sup">') == 1
    assert 'class="summary"' not in html
    rows = re.findall(r'<div><dt>([^<]*)</dt><dd>([^<]*)</dd></div>', html)
    assert rows == [
        ('Game', 'Test Game'),
        ('Format', 'Highscore · No knockout'),
        ('Team size', '2'),
        ('Limit', '8'),
        ('Tournament start', '[dd.MM. HH:mm] 2026-06-01'),
        ('End', '[dd.MM. HH:mm] 2026-06-01'),
    ]
    assert 'class="sh"' not in html
    assert 'Overview' not in html


def test_bote_frozen_solo_team_size_reads_solo(bote_env):
    tournament_request = _make_real_request(
        status=TournamentRequestStatus.accepted
    )
    assert tournament_request.team_size == 1

    html = _render_propose_form(
        bote_env,
        mode='frozen',
        form=None,
        tournament_request=tournament_request,
    )

    assert '<dt>Team size</dt><dd>1 (Solo)</dd>' in html


def test_bote_frozen_back_button_is_full_width_below_the_summary(bote_env):
    html = _render_propose_form(
        bote_env,
        mode='frozen',
        form=None,
        tournament_request=_make_tournament_request(status='accepted'),
    )

    assert (
        '<div class="btns" style="margin-top:22px">'
        '\n    <a class="button btn-full" href="/my_requests">'
    ) in html
    assert html.index('</dl>') < html.index('class="btns"')


def test_bote_frozen_lock_note_names_the_decision_date(bote_env):
    env = _make_env({'propose_form': _snippet(_BOTE_PROPOSE_TEMPLATE)})
    env.filters['dateformat'] = lambda dt, fmt=None, **k: f'<{fmt}>'

    html = _render_propose_form(
        env,
        mode='frozen',
        form=None,
        tournament_request=_make_tournament_request(status='accepted'),
    )

    assert (
        'The orga accepted your request on &lt;dd.MM.&gt; and is setting'
        in html
    )
    assert '%(' not in html


def test_bote_frozen_short_dates_render_the_drafts_forms_with_babel():
    """The real CLDR patterns render the summary rows and lock note."""
    from babel import dates

    env = _make_env({'propose_form': _snippet(_BOTE_PROPOSE_TEMPLATE)})
    env.filters['datetimeformat'] = lambda dt, fmt: dates.format_datetime(
        dt, fmt, locale='de'
    )
    env.filters['dateformat'] = lambda dt, fmt='medium': dates.format_date(
        dt, fmt, locale='de'
    )
    tournament_request = dataclasses.replace(
        _make_real_request(status=TournamentRequestStatus.accepted),
        decided_at=datetime(2026, 9, 27, 10, 12),
        preferred_start_time=datetime(2026, 10, 3, 21, 0),
        preferred_end_time=datetime(2026, 10, 3, 23, 0),
    )

    html = _render_propose_form(
        env, mode='frozen', form=None, tournament_request=tournament_request
    )

    assert '<dt>Tournament start</dt><dd>03.10. 21:00</dd>' in html
    assert '<dt>End</dt><dd>03.10. 23:00</dd>' in html
    assert 'accepted your request on 27.09. and is setting up' in html


def test_bote_partial_drops_the_transitional_rules_of_the_old_markup():
    css = _strip_css_comments(_BOTE_STYLE_PARTIAL.read_text())

    for selector in ('.summary', '.desk .sh', '.warn h2', '.request-page .cap'):
        assert selector not in css, selector


def _css_rule_body(css: str, selector: str) -> str:
    match = re.search(re.escape(selector) + r' \{([^}]*)\}', css)
    assert match, selector
    return match.group(1)


def test_bote_dashboard_section_heads_are_not_tied_to_the_form_wrapper():
    """The dashboard's `.sh` heads sit in no `.fs` wrapper."""
    css = _strip_css_comments(_BOTE_STYLE_PARTIAL.read_text())

    assert '.fs .sh' not in css
    assert 'justify-content: space-between' in _css_rule_body(
        css, '.request-page .sh'
    )
    assert 'font: 900 26px' in _css_rule_body(css, '.request-page .sh h2')
    assert 'white-space: nowrap' in _css_rule_body(
        css, '.request-page .sh > span'
    )
    assert 'margin-top: 30px' in _css_rule_body(css, '.request-page .sh.sh-top')


def test_bote_dashboard_rules_match_the_drafts_computed_values():
    """The dashboard rules match the values the harness measures."""
    css = _strip_css_comments(_BOTE_STYLE_PARTIAL.read_text())

    win = _css_rule_body(css, '.request-page .req.win')
    assert 'display: grid' in win
    assert 'grid-template-columns: minmax(0, 1fr)' in win
    assert 'gap: 6px 20px' in win
    assert 'margin-top: 0' in _css_rule_body(
        css, '.request-page .sh + .req.win'
    )
    assert 'line-height: normal' in _css_rule_body(css, '.request-page .trk li')
    assert 'font-weight: 400' in _css_rule_body(
        css, '.request-page .empty-box p'
    )


def test_bote_link_rules_outrank_the_themes_underline_draw():
    """The `.lnk` underline and button-link reset outrank the theme."""
    theme = _strip_css_comments(_THEME_CSS_PATH.read_text())
    css = _strip_css_comments(_BOTE_STYLE_PARTIAL.read_text())

    theme_selector = _extract_selector_for_rule_with(
        theme, 'background-size: 0 1px'
    )
    lnk_match = re.search(
        r'(\.request-page a\.lnk:not\(#[\w-]+\))\s*\{\s*'
        r'text-decoration: underline;',
        css,
    )
    assert lnk_match, 'no `.lnk` underline rule outranking the theme'
    lnk_selector = lnk_match.group(1)
    button_selector = _extract_selector_for_rule_with(
        css, 'background-size: auto;'
    )

    theme_specificity = _css_specificity(theme_selector)
    assert theme_specificity == (0, 7, 2)
    assert _css_specificity(lnk_selector) > theme_specificity, lnk_selector
    assert (
        _css_specificity(
            button_selector.replace(':is(.button, .lnk)', '.button')
        )
        > theme_specificity
    ), button_selector
    assert lnk_selector.startswith('.request-page ')
    assert button_selector.startswith('.request-page ')
    assert 'background-repeat: repeat;' in css
    assert 'background-position: 0% 0%;' in css


def test_bote_before_prefix_is_generated_content_of_the_caption():
    """The "Before" prefix is generated content, not a child node."""
    css = _strip_css_comments(_BOTE_STYLE_PARTIAL.read_text())

    selector = _extract_selector_for_rule_with(
        css, 'content: attr(data-before);'
    )

    assert selector == (
        '.request-page [data-limit-caption][data-before]::before'
    )
    assert '.request-page [data-limit-caption].over' in css


def test_bote_slip_padding_and_mobile_reset_match_the_draft():
    css = _strip_css_comments(_BOTE_STYLE_PARTIAL.read_text())

    assert 'padding: 36px 44px 40px;' in css
    mobile_start = css.index('@media (max-width: 720px)')
    mobile_rule = css[mobile_start : css.index('}', mobile_start) + 1]
    assert 'max-width: none;' in mobile_rule
    assert 'padding: 18px 16px 32px;' in mobile_rule


def test_bote_blue_token_is_the_drafts_and_dark_keeps_the_themes():
    css = _strip_css_comments(_BOTE_STYLE_PARTIAL.read_text())

    light = re.search(r'\n  \.request-page \{([^}]*)\}', css)
    dark = re.search(
        r'html\[data-theme="dark"\] \.request-page \{([^}]*)\}', css
    )
    assert light and '--blue: #1f4470;' in light.group(1)
    assert dark and '--blue: #8fa9d2;' in dark.group(1)
