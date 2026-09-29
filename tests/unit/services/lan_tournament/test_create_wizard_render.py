"""
tests.unit.services.lan_tournament.test_create_wizard_render
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from datetime import datetime
import json
import pathlib
import re
from types import SimpleNamespace
from uuid import uuid4

from flask import Flask
from flask_babel import Babel, format_number
from jinja2 import DictLoader, Environment, StrictUndefined
from markupsafe import Markup
import pytest
from werkzeug.datastructures import MultiDict

from byceps.services.lan_tournament.blueprints.admin.forms import (
    TournamentCreateForm,
)
from byceps.services.lan_tournament.lan_tournament_view_helpers import (
    build_create_wizard_context,
    build_create_wizard_strings,
    build_request_refusal,
    CREATE_WIZARD_STEP_FIELDS,
    first_error_step,
)
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.tournament_image import (
    TournamentImage,
    TournamentImageID,
)
from byceps.services.lan_tournament.models.tournament_request import (
    TournamentRequestStatus,
)
from byceps.util.image.image_type import ImageType


_TEMPLATE = pathlib.Path(
    'byceps/services/lan_tournament/blueprints/admin/templates'
    '/admin/lan_tournament/create_form.html'
)
_FORMS_MACRO_SRC = pathlib.Path(
    'byceps/services/core/blueprints/common/templates/macros/forms.html'
).read_text()

_LAYOUT_STUB = (
    '{% block head %}{% endblock %}'
    '<div id="before-body">{% block before_body %}{% endblock %}</div>'
    '{% block body %}{% endblock %}'
    '{% block scripts %}{% endblock %}'
)
_ADMIN_MACROS_STUB = (
    '{% macro render_backlink(url, label) -%}'
    '<a class="backlink" href="{{ url }}">{{ label }}</a>'
    '{%- endmacro %}'
)
_ICONS_STUB = (
    '{% macro render_icon(name, color=None, title=None, '
    "filename='icons') %}{% endmacro %}"
)

_URLS = {
    'create': '/create',
    'upload': '/upload',
    'delete_template': '/images/__ID__',
    'images': '/images',
    'validate': '/create/validate',
    'cancel': '/cancel',
}

# The `before_body` block: F-17's back link target and text, drafted style.
_BEFORE_BODY = """{% block before_body %}
{% if source_request %}
<a class="lt-wiz-backlink" href="{{ url_for('.view_request', request_id=source_request.id) }}">← {{ _('Tournament request #%(number)s', number='%04d'|format(source_request.number)) }}</a>
{% else %}
<a class="lt-wiz-backlink" href="{{ url_for('.index', party_id=party.id) }}">‹ {{ _('Tournaments') }}</a>
{% endif %}
{%- endblock %}"""


@pytest.fixture(scope='module')
def app():
    a = Flask(__name__)
    a.config['TESTING'] = True
    a.config['LOCALE'] = 'en'
    a.config['BABEL_DEFAULT_LOCALE'] = 'en'
    a.config['TIMEZONE'] = 'Europe/Berlin'
    Babel(a)
    return a


def _url_for(endpoint, **kwargs):
    if endpoint == 'static':
        return '/static/' + kwargs['filename']
    return '/' + endpoint.lstrip('.')


def _user_with_permissions(permissions):
    return SimpleNamespace(
        user=SimpleNamespace(has_permission=lambda p: p in permissions)
    )


@pytest.fixture(scope='module')
def env():
    e = Environment(
        undefined=StrictUndefined,
        autoescape=True,
        loader=DictLoader(
            {
                'create_form.html': _TEMPLATE.read_text(),
                'layout/admin/lan_tournament.html': _LAYOUT_STUB,
                'macros/admin.html': _ADMIN_MACROS_STUB,
                'macros/forms.html': _FORMS_MACRO_SRC,
                'macros/icons.html': _ICONS_STUB,
            }
        ),
    )
    e.globals['g'] = _user_with_permissions({'user.view'})
    e.globals['_'] = lambda s, **kw: (Markup(s) % kw) if kw else s  # noqa: S704
    e.globals['ngettext'] = lambda s, p, num, **kw: (s if num == 1 else p) % kw
    e.globals['url_for'] = _url_for
    e.filters['dateformat'] = lambda dt, *a, **k: dt.strftime('%Y-%m-%d')
    e.filters['numberformat'] = format_number
    e.filters['timeformat'] = lambda dt, *a, **k: dt.strftime('%H:%M')
    return e


@pytest.fixture
def ctx(app):
    with app.app_context(), app.test_request_context('/'):
        yield


@pytest.fixture(scope='module')
def german_app():
    a = Flask(__name__)
    a.config['TESTING'] = True
    a.config['LOCALE'] = 'de'
    a.config['BABEL_DEFAULT_LOCALE'] = 'de'
    a.config['BABEL_DEFAULT_TIMEZONE'] = 'Europe/Berlin'
    a.config['TIMEZONE'] = 'Europe/Berlin'
    Babel(a)
    return a


@pytest.fixture
def german_ctx(german_app):
    with german_app.app_context(), german_app.test_request_context('/'):
        yield


def _party(**overrides):
    values = {
        'id': 'p1',
        'title': 'Party 36',
        'starts_at': datetime(2026, 10, 1, 12, 0),
        'ends_at': datetime(2026, 10, 4, 12, 0),
        'max_ticket_quantity': 120,
    }
    return SimpleNamespace(**(values | overrides))


def _source_request(**overrides):
    values = {
        'id': 'r1',
        'number': 142,
        'team_size': 2,
        'participant_limit': 12,
        'preferred_end_time': datetime(2026, 10, 3, 20, 0),
        'preferred_start_time': datetime(2026, 10, 3, 14, 0),
        'proposer_id': 'u9',
        'name': 'Rollator-Rallye 2026',
        'game': 'Mario Kart 8 Deluxe',
        'description': 'Drei Cups.',
        'special_rules': 'Keine blauen Panzer.',
        'game_format': GameFormat.FREE_FOR_ALL,
        'elimination_mode': EliminationMode.SINGLE_ELIMINATION,
    }
    return SimpleNamespace(**(values | overrides))


def _make_form(data=None, *, validate=False):
    form = TournamentCreateForm(formdata=MultiDict(data) if data else None)
    form.set_contestant_type_choices()
    form.set_game_format_choices()
    form.set_elimination_mode_choices()
    form.set_score_ordering_choices()
    if validate:
        form.validate()
    return form


def _staged_image(filename='banner.png'):
    return TournamentImage(
        id=TournamentImageID(uuid4()),
        party_id='p1',
        creator_id='u1',
        created_at=datetime(2026, 9, 29, 10, 0),
        filename=filename,
        image_type=ImageType.png,
        width=1920,
        height=1080,
        byte_size=123456,
    )


def _render(
    env,
    form,
    *,
    source_request=None,
    proposer='Oma_Gerda',
    staged_image=None,
    blocking=(),
    party=None,
    refusal=None,
    refused_request=None,
):
    party = party or _party()
    wizard = build_create_wizard_context(
        party,
        form,
        source_request=source_request,
        source_proposer_name=proposer if source_request else None,
        staged_image=staged_image,
        urls=_URLS,
        refusal=refusal,
    )
    return env.get_template('create_form.html').render(
        party=party,
        form=form,
        wizard=wizard,
        source_request=source_request,
        source_proposer_name=proposer if source_request else None,
        source_request_blocking_field_labels=list(blocking),
        refused_request=refused_request,
        refused_proposer_name=proposer if refused_request else None,
    )


def _island(out):
    match = re.search(
        r'<script type="application/json" id="lt-create-wizard-config">'
        r'(.*?)</script>',
        out,
        re.DOTALL,
    )
    assert match is not None
    return match.group(1)


def test_create_form_has_no_inline_display_none(env, ctx):
    src = _TEMPLATE.read_text()
    out = _render(env, _make_form())

    pattern = re.compile(r'display\s*:\s*none')
    assert pattern.search(src) is None
    assert pattern.search(out) is None
    assert 'style=' not in out


def test_create_form_renders_five_steps_and_scope_legends(env, ctx):
    out = _render(env, _make_form())

    assert re.findall(r'data-wiz-step="(\d)"', out) == list('01234')
    assert re.findall(r'<fieldset class="lt-wiz-step[^"]*" data-wiz-step', out)
    assert '<section class="lt-wiz-step box" data-wiz-step="4">' in out
    assert re.findall(r'data-wiz-scope="([a-z-]+)"', out) == [
        'solo',
        'team',
        'team-players',
        'one-v-one',
        'highscore',
        'ffa',
        'ffa-de',
        'ffa-preview',
    ]
    for legend in (
        '<span class="lt-wiz-legend-js">Solo · players</span>',
        '<span class="lt-wiz-legend-js">Teams</span>',
        'Players per team',
        'Only for Double Elimination',
    ):
        assert legend in out
    assert '<legend>Players per team</legend>' in out
    assert '<legend>Only for Double Elimination</legend>' in out
    for retired in (
        'Only for Solo',
        'Only for teams',
        'Only for Highscore',
        'Only for Free-for-All',
    ):
        assert f'<legend>{retired}</legend>' not in out
    assert '1 · Basics' in out
    assert '5 · Review and create' in out


def _card(out, field, value):
    """Return the markup of one choice card."""
    match = re.search(
        rf'<label class="lt-wiz-card"[^>]*>\s*<input type="radio" '
        rf'name="{field}" value="{value}".*?</label>',
        out,
        re.S,
    )
    assert match, (field, value)
    return match.group(0)


def test_create_form_choice_cards_use_the_drafted_names(env, ctx):
    out = _render(env, _make_form())

    for legend in ('Who competes?', 'Tournament mode', 'Score sorting'):
        assert re.search(rf'<legend class="form-label">{legend}\b', out)
    for shared in ('Contestant type', 'Elimination mode'):
        assert f'<legend class="form-label">{shared}' not in out
    for value, title in (
        ('SINGLE_ELIMINATION', 'Single knockout'),
        ('DOUBLE_ELIMINATION', 'Double knockout'),
        ('ROUND_ROBIN', 'Everyone plays everyone'),
        ('NONE', 'No knockout'),
    ):
        card = _card(out, 'elimination_mode', value)
        assert f'lt-wiz-card__title">{title}<' in card
    assert 'lt-wiz-card__title">Teams<' in _card(out, 'contestant_type', 'TEAM')


def test_create_form_format_cards_show_example_chips(env, ctx):
    out = _render(env, _make_form())

    one = _card(out, 'game_format', 'ONE_V_ONE')
    assert '<span class="lt-wiz-chip">A</span> against ' in one
    assert '<span class="lt-wiz-chip">B</span> → winner advances' in one
    ffa = _card(out, 'game_format', 'FREE_FOR_ALL')
    assert ffa.count('lt-wiz-chip') == 4
    assert '→ places 1–4 → points' in ffa
    high = _card(out, 'game_format', 'HIGHSCORE')
    assert re.findall(r'lt-wiz-chip">([^<]*)<', high) == [
        'A 12,400',
        'B 11,950',
        'C 9,800',
    ]


def test_create_form_mode_area_holds_hint_fixed_row_and_reasons(env, ctx):
    out = _render(env, _make_form())

    assert re.search(
        r'<div class="empty-hint" data-wiz-hint hidden>Choose a game format '
        r'first\. Then only matching modes are shown\.</div>',
        out,
    )
    assert re.search(
        r'<div class="lt-wiz-fixed" data-wiz-fixed hidden>\s*'
        r'<span class="tag outl">automatic</span>\s*'
        r'<span><strong>No elimination\.</strong> Highscore has no matches',
        out,
    )
    assert 'data-wiz-card-nojs' in _card(out, 'elimination_mode', 'NONE')
    assert 'data-wiz-card-nojs' not in _card(
        out, 'elimination_mode', 'SINGLE_ELIMINATION'
    )
    round_robin = _card(out, 'elimination_mode', 'ROUND_ROBIN')
    assert re.search(
        r'data-wiz-why hidden>Not available: Only for 1v1<', round_robin
    )
    assert 'data-wiz-ex="FREE_FOR_ALL"' not in round_robin
    single = _card(out, 'elimination_mode', 'SINGLE_ELIMINATION')
    assert 'data-wiz-ex="ONE_V_ONE" hidden' in single
    assert 'data-wiz-ex="FREE_FOR_ALL" hidden' in single


def test_create_form_marks_only_the_checked_card_as_selected(env, ctx):
    form = _make_form(
        {'contestant_type': 'TEAM', 'game_format': 'FREE_FOR_ALL'}
    )
    out = _render(env, form)

    team = _card(out, 'contestant_type', 'TEAM')
    solo = _card(out, 'contestant_type', 'SOLO')
    assert re.search(r'data-wiz-selected-sr> \(selected\)<', team)
    assert re.search(r'data-wiz-selected-sr hidden> \(selected\)<', solo)


def test_create_form_participant_fields_carry_placeholders_and_captions(
    env, ctx
):
    out = _render(env, _make_form(), party=_party())

    # min_players, min_teams, min_players_in_team and the advancement.
    assert out.count('placeholder="–"') == 4
    # max_players, max_teams and max_players_in_team.
    assert out.count('placeholder="no limit"') == 3
    assert re.search(r'id="group_size_min"[^>]*placeholder="2"', out)
    assert out.count('Displayed on the tournament page.') == 2
    assert 'Shown on the tournament page.' in out  # step 1 description
    for name, label in (
        ('min_players_in_team', 'Minimum'),
        ('max_players_in_team', 'Maximum'),
    ):
        assert re.search(
            rf'data-wiz-field="{name}" data-wiz-label="[^"]+">\s*'
            rf'<label class="form-label" for="{name}">{label}\b',
            out,
        )


def test_create_form_capacity_lines_sit_inside_their_fieldsets(env, ctx):
    out = _render(env, _make_form(), party=_party())

    solo = re.search(
        r'<fieldset class="lt-wiz-scope" data-wiz-scope="solo">.*?</fieldset>',
        out,
        re.S,
    ).group(0)
    players = re.search(
        r'<fieldset class="lt-wiz-scope" data-wiz-scope="team-players">'
        r'.*?</fieldset>',
        out,
        re.S,
    ).group(0)
    assert 'data-wiz-capacity' in solo
    assert 'No limit: up to 120 party seats.' in solo
    assert 'data-wiz-capacity' in players
    assert 'there is no limit (party: 120 seats)' in players
    assert out.count('data-wiz-capacity') == 2


def test_create_form_step_four_has_no_scope_fieldsets_around_its_parts(
    env, ctx
):
    out = _render(env, _make_form())
    step = re.search(
        r'<fieldset class="lt-wiz-step box" data-wiz-step="3">.*?</fieldset>\s*'
        r'<section',
        out,
        re.S,
    ).group(0)

    assert '<fieldset class="lt-wiz-cards' in step  # the score cards
    for wrapper in ('highscore', 'ffa', 'one-v-one'):
        assert (
            f'<fieldset class="lt-wiz-scope" data-wiz-scope="{wrapper}"'
            not in step
        )
    assert '<legend>Groups · size in teams or players</legend>' in step
    assert 'Advancing per group' in step
    assert 'Only for Double Elimination' in step
    order = [
        step.index(marker)
        for marker in (
            'data-ffa-editor',
            'lt-wiz-groups',
            'data-wiz-scope="ffa-de"',
            'data-wiz-preview',
        )
    ]
    assert order == sorted(order)


def test_create_form_one_v_one_box_starts_hidden_and_explains_the_skip(
    env, ctx
):
    out = _render(env, _make_form())
    box = re.search(
        r'<div data-wiz-scope="one-v-one" hidden>.*?</div>\s*<p[^>]*>.*?</p>',
        out,
        re.S,
    ).group(0)

    assert '<span class="tag outl">nothing to set</span>' in box
    assert '<strong>1v1 · <span data-wiz-1v1-mode>–</span></strong>.' in box
    assert 'The bracket is generated before the start.' in box
    assert 'This step is skipped for 1v1.' in box


def test_create_form_carry_option_spells_out_both_states(env, ctx):
    out = _render(env, _make_form())

    assert (
        '<span><strong>Carry points into the losers bracket</strong></span>'
        in out
    )
    assert re.search(
        r'<strong>On:</strong> All points from the winners and losers '
        r'brackets count for seeding into the final\.<br>'
        r'<strong>Off:</strong> In the losers bracket only points',
        out,
    )


def test_create_form_point_editor_is_a_labelled_group(env, ctx):
    out = _render(env, _make_form())

    assert re.search(
        r'data-ffa-editor data-no-drag role="group" '
        r'aria-labelledby="point_table-label"',
        out,
    )
    assert re.search(
        r'<div class="form-label" id="point_table-label">Points by placement',
        out,
    )
    assert (
        'data-wiz-empty="No points yet. Choose a template or add places."'
        in out
    )
    assert '<span class="dimmed lt-wiz-toolbar-label">Template:</span>' in out


_SHARED_MSGIDS = (
    'Single Elimination',
    'Double Elimination',
    'Round Robin',
    'None',
    'Elimination mode',
    'Contestant type',
    'Advance per group',
    'Points carry to losers',
    'Only available for %(format)s',
    'Score ordering',
)


def test_create_form_uses_its_own_mode_msgids_not_the_shared_ones():
    source = _TEMPLATE.read_text()

    used = set(re.findall(r"_\('((?:[^'\\]|\\.)*)'", source))
    assert used.isdisjoint(_SHARED_MSGIDS), sorted(used & set(_SHARED_MSGIDS))


def test_create_form_reaches_every_field_without_js(env, ctx):
    out = _render(env, _make_form())

    for name in CREATE_WIZARD_STEP_FIELDS[0] + CREATE_WIZARD_STEP_FIELDS[3]:
        assert f'name="{name}"' in out, name
    for name in (
        'min_players',
        'max_players',
        'min_teams',
        'max_teams',
        'min_players_in_team',
        'max_players_in_team',
    ):
        assert f'name="{name}"' in out, name

    noscript = out[out.index('<noscript>') : out.index('</noscript>')]
    assert ' name="point_table"' in noscript
    hidden_from = out.index('data-wiz-js-only hidden')
    assert hidden_from < out.index('<noscript>')
    assert (
        ' name="point_table"' not in out[hidden_from : out.index('<noscript>')]
    )


def test_create_form_is_multipart_with_hidden_fields(env, ctx):
    form = _make_form()
    form.from_request_id.data = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
    out = _render(env, form)

    form_tag = re.search(r'<form [^>]*>', out).group(0)
    assert 'action="/create"' in form_tag
    assert 'method="post"' in form_tag
    assert 'enctype="multipart/form-data"' in form_tag
    assert 'data-lt-wizard' in form_tag

    hidden = re.findall(r'<input [^>]*type="hidden"[^>]*>', out)
    names = [re.search(r'name="([^"]+)"', tag).group(1) for tag in hidden]
    assert names == ['from_request_id', 'submission_token', 'image_id']
    assert 'value="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"' in hidden[0]


def test_create_form_has_exactly_one_submit_button(env, ctx):
    for form in (_make_form(), _make_form({'name': ''}, validate=True)):
        out = _render(env, form)

        assert out.count('type="submit"') == 1
        assert re.search(r'<input[^>]*type="submit"', out) is None
        assert re.search(r'<button(?![^>]*\btype=)', out) is None
        assert 'data-wiz-submit' in out
        assert re.search(r'<button type="submit"[^>]*data-wiz-submit', out)


def test_create_form_renders_field_errors_and_summary_links(env, ctx):
    form = _make_form({'name': '   ', 'min_players': 'abc'}, validate=True)
    form.from_request_id.errors.append('The request is gone.')
    form.form_errors.append('Something is wrong overall.')

    out = _render(env, form)

    summary = out[
        out.rindex('<div', 0, out.index('data-wiz-errsum')) : out.index(
            '<form action='
        )
    ]
    assert 'role="alert"' in summary
    assert 'tabindex="-1"' in summary
    assert '<a href="#name">Name</a>: Please enter a name.' in summary
    assert '<a href="#min_players">Min. players</a>' in summary
    assert 'Whole numbers from 1 only.' in summary
    assert 'href="#from_request_id"' not in summary
    assert 'The request is gone.' not in summary
    assert 'Something is wrong overall.' not in summary
    assert '2 entries need attention' in summary

    name_block = re.search(
        r'data-wiz-field="name".*?data-wiz-err-for="name"[^>]*>(.*?)</div>',
        out,
        re.DOTALL,
    )
    assert 'Please enter a name.' in name_block.group(1)
    review = out[out.index('data-wiz-step="4"') :]
    assert 'The request is gone.' in review
    assert 'Something is wrong overall.' in review
    assert 'data-first-error-step="0"' in out
    visible = re.sub(r'data-label-\w+="[^"]*"', '', out).replace(
        _island(out), ''
    )
    assert '%(' not in visible


def test_create_form_summary_count_is_singular_for_one_error(env, ctx):
    form = _make_form({'name': '   '}, validate=True)

    out = _render(env, form)

    assert '1 entry needs attention' in out
    assert '1 entries need attention' not in out


def test_create_form_summary_leaves_out_what_the_review_box_shows(env, ctx):
    form = _make_form()
    form.from_request_id.errors = ['The request is gone.']
    form.form_errors.append('Something is wrong overall.')

    out = _render(env, form)

    assert 'data-wiz-errsum' not in out.replace('[data-wiz-errsum]', '')
    review = out[out.index('data-wiz-step="4"') :]
    assert '<li>The request is gone.</li>' in review
    assert '<li>Something is wrong overall.</li>' in review


def test_create_form_renders_no_summary_without_errors(env, ctx):
    out = _render(env, _make_form())

    assert 'data-wiz-errsum' not in out.replace('[data-wiz-errsum]', '')
    assert 'data-first-error-step' not in out


def test_create_form_config_island_is_json_and_script_safe(env, ctx):
    hostile = '</script><script>alert(1)</script>'
    form = _make_form({'name': hostile}, validate=True)
    form.name.errors.append(hostile)

    out = _render(
        env,
        form,
        source_request=_source_request(),
        proposer=hostile,
        staged_image=_staged_image(filename=hostile),
    )

    island = _island(out)
    assert '</script>' not in island
    assert '<' not in island

    config = json.loads(island)
    assert config['request']['proposerName'] == hostile
    assert config['stagedImage']['filename'] == hostile
    assert config['serverErrors']['name'] == [hostile]
    assert '<script>alert(1)' not in out


def test_create_form_renders_request_aside_when_source_request(env, ctx):
    out = _render(
        env,
        _make_form(),
        source_request=_source_request(),
        blocking=('Game',),
    )

    aside = out[
        out.index('<aside class="lt-wiz-aside">') : out.index('</aside>')
    ]
    assert aside.index('Prefilled from request #0142 by Oma_Gerda.') < (
        aside.index('Party 36')
    )
    assert 'Preferred end (not a field here):' in aside
    assert (
        'Still missing before this request can become a tournament: Game.'
        in (aside)
    )
    assert 'notification color-info' in aside

    assert 'Tournament request #0142' in out[: out.index('<h1')]
    assert 'href="/view_request"' in out[: out.index('<h1')]
    assert 'data-wiz-reqmap' in out
    assert (
        'Team size 2 → min./max. players per team = 2 · Limit 12 → max. teams = 12'
        in out
    )


def test_create_form_omits_request_box_without_source_request(env, ctx):
    out = _render(env, _make_form())

    aside = out[
        out.index('<aside class="lt-wiz-aside">') : out.index('</aside>')
    ]
    assert 'Prefilled from request' not in aside
    assert 'Preferred end' not in out
    assert 'data-wiz-reqmap' not in out.replace('[data-wiz-reqmap]', '')
    assert 'href="/index"' in out[: out.index('<h1')]


def test_create_form_reqmap_line_for_solo_request(env, ctx):
    form = _make_form({'contestant_type': 'SOLO'})

    out = _render(env, form, source_request=_source_request(team_size=1))

    assert 'Limit 12 → max. players = 12' in out
    assert 'max. teams = 12' not in out


def test_create_form_renders_radio_cards_with_enum_names(env, ctx):
    form = _make_form({'game_format': 'FREE_FOR_ALL'})

    out = _render(env, form)

    for field, values in {
        'contestant_type': ['SOLO', 'TEAM'],
        'game_format': ['ONE_V_ONE', 'FREE_FOR_ALL', 'HIGHSCORE'],
        'elimination_mode': [
            'SINGLE_ELIMINATION',
            'DOUBLE_ELIMINATION',
            'ROUND_ROBIN',
            'NONE',
        ],
        'score_ordering': ['HIGHER_IS_BETTER', 'LOWER_IS_BETTER'],
    }.items():
        assert f'data-wiz-choice="{field}"' in out
        found = re.findall(
            rf'<input type="radio" name="{field}" value="(\w+)"', out
        )
        assert found == values
    assert (
        '<input type="radio" name="game_format" value="FREE_FOR_ALL" checked>'
        in out
    )
    assert out.count(' checked>') == 1


def test_create_form_renders_counts_as_text_inputs(env, ctx):
    out = _render(env, _make_form())

    for name in ('min_players', 'max_teams', 'group_size_max'):
        tag = re.search(rf'<input [^>]*name="{name}"[^>]*>', out).group(0)
        assert 'type="text"' in tag
        assert 'inputmode="numeric"' in tag


def test_create_form_renders_staged_image_preview(env, ctx):
    image = _staged_image()

    out = _render(env, _make_form(), staged_image=image)

    assert f'src="/data/parties/p1/lan_tournament/images/{image.id}.png"' in out
    assert 'banner.png' in out
    assert '1920 × 1080' in out
    assert 'data-wiz-image-staged' not in _render(env, _make_form())


def test_create_form_image_block_hooks(env, ctx):
    out = _render(env, _make_form())

    file_input = re.search(r'<input [^>]*type="file"[^>]*>', out).group(0)
    assert 'name="image"' in file_input
    assert 'data-wiz-file' in file_input
    assert 'accept="image/jpeg,image/png,image/webp"' in file_input
    assert re.search(r'<button [^>]*data-wiz-lib-open[^>]*hidden', out)
    assert '<details class="lt-wiz-urlalt">' in out
    assert out.index('<details') < out.index('name="image_url"')
    assert out.index('name="image_url"') < out.index('</details>')
    assert out.index('</details>') < out.index('name="image_alt_text"')


def test_create_form_ffa_editor_uses_ffa_js_hooks(env, ctx):
    out = _render(env, _make_form({'point_table': '10,8,6'}))

    editor = re.search(r'<div class="ffa-point-editor"[^>]*>', out).group(0)
    assert 'data-ffa-editor' in editor
    assert 'data-no-drag' in editor
    assert 'data-field-name="point_table"' in editor
    assert 'data-field-value="10,8,6"' in editor
    assert 'data-label-points="Points for place %(n)s"' in editor
    assert 'data-label-remove="Remove place %(n)s"' in editor


def test_create_form_loads_scripts_in_order(env, ctx):
    out = _render(env, _make_form())

    order = [
        'behavior/lan_tournament_ffa.js',
        'behavior/lan_tournament_create_wizard_rules.js',
        'behavior/lan_tournament_create_wizard_image.js',
        'behavior/lan_tournament_create_wizard.js',
    ]
    positions = [out.index(name) for name in order]
    assert positions == sorted(positions)
    assert 'style/lan_tournament_create_wizard.css' in _TEMPLATE.read_text()


def test_build_create_wizard_context_keys(ctx):
    form = _make_form()

    wizard = build_create_wizard_context(
        _party(),
        form,
        source_request=_source_request(),
        source_proposer_name='Oma_Gerda',
        staged_image=_staged_image(),
        urls=_URLS,
    )

    assert set(wizard) == {
        'config',
        'timezone_label',
        'timezone_name',
        'timezone_detail',
        'capacity',
        'party_dates',
        'staged_image',
        'first_error_step',
        'server_banner',
        'refusal',
        'source_values',
    }
    assert re.fullmatch(
        r'Europe/Berlin \(UTC\+0[12]:00\)', wizard['timezone_label']
    )
    assert wizard['capacity'] == 120
    assert wizard['party_dates'] == '2026-10-01 – 2026-10-04' or (
        '–' in wizard['party_dates']
    )
    assert wizard['first_error_step'] is None
    assert set(wizard['staged_image']) == {'url', 'filename', 'width', 'height'}

    config = wizard['config']
    assert set(config) == {
        'sessionKey',
        'capacity',
        'timezoneLabel',
        'timezoneName',
        'timezoneDetail',
        'locale',
        'limits',
        'validCombinations',
        'urls',
        'request',
        'refusal',
        'sourceValues',
        'stagedImage',
        'firstErrorStep',
        'serverErrors',
        'strings',
    }
    assert config['sessionKey'] == 'lt-create:p1:r1'
    assert config['sourceValues'] == wizard['source_values']
    assert config['sourceValues'] == {
        'name': 'Rollator-Rallye 2026',
        'game': 'Mario Kart 8 Deluxe',
        'description': 'Drei Cups.',
        'ruleset': 'Keine blauen Panzer.',
        'start_time': config['sourceValues']['start_time'],
        'game_format': 'FREE_FOR_ALL',
        'elimination_mode': 'SINGLE_ELIMINATION',
    }
    assert re.fullmatch(
        r'2026-10-03T1[4-6]:00', config['sourceValues']['start_time']
    )
    assert config['urls'] == _URLS
    assert config['limits'] == {
        'nameMax': 80,
        'gameMax': 80,
        'textMax': 10000,
        'altMax': 200,
        'pointTableMax': 64,
        'pointValueMax': 999_999_999,
        'countMax': 1024,
        'uploadBytes': 5 * 1024 * 1024,
        'minWidth': 960,
        'minHeight': 540,
        'maxWidth': 8000,
        'maxHeight': 8000,
    }
    assert config['validCombinations'] == {
        'FREE_FOR_ALL': ['DOUBLE_ELIMINATION', 'SINGLE_ELIMINATION'],
        'HIGHSCORE': ['NONE'],
        'ONE_V_ONE': [
            'DOUBLE_ELIMINATION',
            'ROUND_ROBIN',
            'SINGLE_ELIMINATION',
        ],
    }
    assert config['request'] == {
        'number': 142,
        'proposerName': 'Oma_Gerda',
        'teamSize': 2,
        'participantLimit': 12,
        'derivedContestantType': 'TEAM',
    }
    assert set(config['stagedImage']) == {
        'imageId',
        'url',
        'filename',
        'width',
        'height',
        'byteSize',
    }
    assert config['serverErrors'] == {}
    assert config['strings'] == build_create_wizard_strings()
    json.dumps(config)


def test_build_create_wizard_context_without_request_or_image(ctx):
    wizard = build_create_wizard_context(
        _party(),
        _make_form(),
        source_request=None,
        source_proposer_name=None,
        staged_image=None,
        urls=_URLS,
    )

    assert wizard['staged_image'] is None
    assert wizard['config']['request'] is None
    assert wizard['config']['stagedImage'] is None
    assert wizard['config']['sessionKey'] == 'lt-create:p1:new'


def test_build_create_wizard_context_derives_solo_from_team_size_one(ctx):
    wizard = build_create_wizard_context(
        _party(),
        _make_form(),
        source_request=_source_request(team_size=1),
        source_proposer_name=None,
        staged_image=None,
        urls=_URLS,
    )

    assert wizard['config']['request']['derivedContestantType'] == 'SOLO'


def test_build_create_wizard_context_maps_server_errors(ctx):
    form = _make_form({'name': ''}, validate=True)
    form.form_errors.append('Overall.')

    wizard = build_create_wizard_context(
        _party(),
        form,
        source_request=None,
        source_proposer_name=None,
        staged_image=None,
        urls=_URLS,
    )

    assert wizard['config']['serverErrors']['name'] == ['Please enter a name.']
    assert wizard['config']['serverErrors'][''] == ['Overall.']
    assert wizard['first_error_step'] == 0
    assert wizard['config']['firstErrorStep'] == 0


def test_build_create_wizard_strings_keys_equal_english_msgids(ctx):
    strings = build_create_wizard_strings()

    assert strings
    assert all(isinstance(value, str) for value in strings.values())
    assert strings['Back'] == 'Back'
    assert strings['Continue: %(step)s'] == 'Continue: %(step)s'
    assert strings['Points for place %(n)s'] == 'Points for place %(n)s'
    assert 'Please enter a name.' in strings


def test_step_fields_cover_every_form_field(ctx):
    form = _make_form()
    flat = [name for step in CREATE_WIZARD_STEP_FIELDS for name in step]

    assert len(flat) == len(set(flat))
    form_fields = {name for name in form._fields if name != 'csrf_token'}
    assert form_fields <= set(flat)
    assert set(flat) - form_fields == {'req_map'}


def test_step_fields_include_req_map_pseudo_field():
    assert 'req_map' in CREATE_WIZARD_STEP_FIELDS[2]
    assert len(CREATE_WIZARD_STEP_FIELDS) == 5


def _fake_form(errors):
    return SimpleNamespace(errors=errors)


def test_first_error_step_picks_earliest_step():
    form = _fake_form(
        {'advancement_count': ['x'], 'game_format': ['y'], 'max_teams': ['z']}
    )

    assert first_error_step(form) == 1


def test_first_error_step_is_none_without_errors():
    assert first_error_step(_fake_form({})) is None


def test_first_error_step_maps_form_errors_to_review():
    assert first_error_step(_fake_form({'': ['boom']})) == 4
    assert first_error_step(_fake_form({'from_request_id': ['gone']})) == 4
    assert first_error_step(_fake_form({'': ['boom'], 'name': ['x']})) == 0


def test_first_error_step_with_a_real_form_error(ctx):
    form = _make_form()
    form.form_errors.append('boom')

    assert first_error_step(form) == 4


def test_create_form_request_box_sits_between_banner_markers():
    src = _TEMPLATE.read_text()
    start_marker = '{# Provenance banner'
    end_marker = '{# /Provenance banner #}'

    assert src.count(start_marker) == 1
    assert src.count(end_marker) == 1
    start = src.index(start_marker)
    end = src.index(end_marker)
    assert start < end
    assert start < src.index('Preferred end (not a field here):') < end
    between = src[start:end]
    assert between.count('{% if source_request %}') == 1
    assert between.count('{% endif %}') == 2  # the request box and the labels
    assert src.index('<aside class="lt-wiz-aside">') < start


def test_create_form_before_body_block_keeps_the_back_link_contract():
    src = _TEMPLATE.read_text()

    start = src.index('{% block before_body %}')
    end = src.index('{%- endblock %}', start) + len('{%- endblock %}')

    assert src[start:end] == _BEFORE_BODY


@pytest.mark.parametrize(
    ('starts_at', 'ends_at', 'expected'),
    [
        (
            datetime(2026, 10, 2, 8),
            datetime(2026, 10, 5, 12),
            '02.10.–05.10.2026',
        ),
        (
            datetime(2026, 12, 31, 8),
            datetime(2027, 1, 2, 12),
            '31.12.2026–02.01.2027',
        ),
        (datetime(2026, 10, 2, 8), datetime(2026, 10, 2, 20), '02.10.2026'),
    ],
)
def test_party_dates_are_a_day_month_range(
    german_ctx, starts_at, ends_at, expected
):
    wizard = build_create_wizard_context(
        _party(starts_at=starts_at, ends_at=ends_at),
        _make_form(),
        source_request=None,
        source_proposer_name=None,
        staged_image=None,
        urls=_URLS,
    )

    assert wizard['party_dates'] == expected


@pytest.mark.parametrize(
    ('starts_at', 'expected'),
    [
        (datetime(2026, 10, 2, 8), 'MESZ, UTC+2'),
        (datetime(2027, 1, 10, 8), 'MEZ, UTC+1'),
    ],
)
def test_timezone_detail_is_taken_at_the_party_start(
    german_ctx, starts_at, expected
):
    wizard = build_create_wizard_context(
        _party(starts_at=starts_at, ends_at=starts_at),
        _make_form(),
        source_request=None,
        source_proposer_name=None,
        staged_image=None,
        urls=_URLS,
    )

    assert wizard['timezone_name'] == 'Europe/Berlin'
    assert wizard['timezone_detail'] == expected


def test_timezone_detail_falls_back_to_the_python_abbreviation(app, ctx):
    wizard = build_create_wizard_context(
        _party(),
        _make_form(),
        source_request=None,
        source_proposer_name=None,
        staged_image=None,
        urls=_URLS,
    )

    assert wizard['timezone_detail'] == 'CEST, UTC+2'


def test_timezone_detail_keeps_the_minutes_of_a_half_hour_offset(
    app, ctx, monkeypatch
):
    monkeypatch.setitem(app.config, 'TIMEZONE', 'Asia/Kolkata')

    wizard = build_create_wizard_context(
        _party(),
        _make_form(),
        source_request=None,
        source_proposer_name=None,
        staged_image=None,
        urls=_URLS,
    )

    assert wizard['timezone_detail'] == 'IST, UTC+5:30'


def test_create_form_aside_omits_the_timezone_detail_of_an_unknown_zone(
    app, env, ctx, monkeypatch
):
    monkeypatch.setitem(app.config, 'TIMEZONE', 'Mars/Base')

    out = _render(env, _make_form())

    aside = out[
        out.index('<aside class="lt-wiz-aside">') : out.index('</aside>')
    ]
    assert '<div class="data-value">Mars/Base</div>' in aside


def test_create_form_aside_shows_the_party_context_as_drafted(env, ctx):
    out = _render(env, _make_form())

    aside = out[
        out.index('<aside class="lt-wiz-aside">') : out.index('</aside>')
    ]
    assert '<div class="data-label">Party seats</div>' in aside
    assert '<div class="data-value">120</div>' in aside
    assert (
        '<div class="data-value">Europe/Berlin '
        '<span class="dimmed">(CEST, UTC+2)</span></div>' in aside
    )
    assert (
        '<span class="tag color-disabled">Draft</span> Registration closed'
        in aside
    )
    assert 'Draft · registration closed' not in aside


def test_create_form_aside_omits_the_seats_without_a_capacity(env, ctx):
    out = _render(env, _make_form(), party=_party(max_ticket_quantity=None))

    aside = out[
        out.index('<aside class="lt-wiz-aside">') : out.index('</aside>')
    ]
    assert 'Party seats' not in aside


def test_create_form_subline_names_party_seats_and_timezone(env, ctx):
    out = _render(env, _make_form())

    assert (
        '<div class="lt-wiz-subline">Party 36 · 120 seats · Europe/Berlin</div>'
        in out
    )
    assert (
        out.index('<h1')
        < out.index('lt-wiz-subline')
        < out.index('lt-wiz-layout')
    )

    without = _render(env, _make_form(), party=_party(max_ticket_quantity=None))

    assert (
        '<div class="lt-wiz-subline">Party 36 · Europe/Berlin</div>' in without
    )


def test_create_form_back_link_uses_the_drafted_style(env, ctx):
    plain = _render(env, _make_form())

    assert (
        '<a class="lt-wiz-backlink" href="/index">‹ Tournaments</a>'
        in plain[: plain.index('<h1')]
    )

    with_request = _render(env, _make_form(), source_request=_source_request())

    assert (
        '<a class="lt-wiz-backlink" href="/view_request">'
        '← Tournament request #0142</a>'
    ) in with_request[: with_request.index('<h1')]


def test_create_form_counter_sits_right_in_the_caption_row(env, ctx):
    out = _render(env, _make_form())

    assert re.search(
        r'<div class="form-caption lt-wiz-caprow" id="description-cap">\s*'
        r'<span>Shown on the tournament page.</span>\s*'
        r'<span class="lt-wiz-count" data-wiz-count-for="description">'
        r'0 / 10,000</span>',
        out,
    )
    assert re.search(
        r'id="name-cap">\s*<span></span>\s*'
        r'<span class="lt-wiz-count" data-wiz-count-for="name">0 / 80</span>',
        out,
    )
    assert 'aria-describedby="name-cap"' in out


def test_create_form_counter_uses_the_locale_number_format(german_ctx, env):
    out = _render(env, _make_form())

    assert '0 / 10.000</span>' in out
    assert '0 / 10,000' not in out


def test_create_form_notices_precede_the_error_summary(env, ctx):
    out = _render(env, _make_form({'name': '   '}, validate=True))

    assert (
        out.index('data-wiz-notices')
        < out.index('data-wiz-errsum')
        < out.index('<form action=')
    )


def test_create_form_nav_puts_the_primary_first_and_a_plain_cancel(env, ctx):
    out = _render(env, _make_form())

    nav = out[out.index('data-wiz-nav') : out.index('</form>')]
    assert nav.index('data-wiz-submit') < nav.index('data-wiz-cancel')
    assert '<a class="button" href="/cancel" data-wiz-cancel>Cancel</a>' in nav
    assert 'is-outlined' not in nav


# Step 1: basics and image


def _tag(out, element_id):
    return re.search(rf'<[^>]*id="{element_id}"[^>]*>', out).group(0)


def test_create_form_text_fields_carry_no_maxlength(env, ctx):
    out = _render(env, _make_form())

    for element_id in ('name', 'game', 'description', 'ruleset'):
        assert 'maxlength' not in _tag(out, element_id), element_id
    assert 'maxlength="200"' in _tag(out, 'image_alt_text')
    assert 'maxlength="256"' in _tag(out, 'image_url')


def test_create_form_over_length_text_keeps_its_server_error(env, ctx):
    form = _make_form({'name': 'x' * 81}, validate=True)

    out = _render(env, form)

    assert 'At most 80 characters – currently 81.' in out
    assert 'maxlength' not in _tag(out, 'name')
    assert '81 / 80' in out


def test_create_form_start_time_sits_in_half_a_row(env, ctx):
    out = _render(env, _make_form())

    assert re.search(
        r'<div class="lt-wiz-row">\s*'
        r'<div class="form-control-block[^"]*" data-wiz-field="start_time">'
        r'.*?</div>\s*<div></div>\s*</div>',
        out,
        re.DOTALL,
    )


def test_create_form_game_and_alt_fields_show_examples(env, ctx):
    out = _render(env, _make_form())

    assert 'placeholder="e.g. Mario Kart 8 Deluxe"' in _tag(out, 'game')
    assert (
        'placeholder="e.g. Logo &#34;Blitz chess by the fireplace&#34; '
        'with a chessboard"'
    ) in _tag(out, 'image_alt_text')


def test_create_form_timezone_caption_names_the_offset(env, ctx):
    out = _render(env, _make_form())

    caption = re.search(
        r'<div class="form-caption" id="start_time-cap">(.*?)</div>',
        out,
        re.DOTALL,
    ).group(1)
    assert re.fullmatch(
        r'In your timezone: Europe/Berlin \((CES?T|MES?Z), UTC\+[12]\)\. '
        r'Party: .+\.',
        caption.strip(),
    )


def test_create_form_keeps_a_visible_file_input_without_js(env, ctx):
    out = _render(env, _make_form())

    drop = out[out.index('data-wiz-drop') :]
    drop = drop[: drop.index('data-wiz-err-for="image"')]
    file_input = re.search(r'<input [^>]*type="file"[^>]*>', drop).group(0)
    assert 'name="image"' in file_input
    assert 'lt-wiz-sr' not in file_input
    assert '<label' not in drop
    assert 'enctype="multipart/form-data"' in out


def test_create_form_alt_field_follows_the_image_url_details(env, ctx):
    out = _render(env, _make_form())

    assert out.index('<details') < out.index('data-wiz-field="image_alt_text"')
    assert out.index('data-wiz-drop') < out.index('<details')


def test_create_form_request_prefill_shows_source_tags(env, ctx):
    source = _source_request()
    form = _make_form()
    form.name.data = source.name
    form.game.data = source.game
    form.description.data = 'Changed by the admin.'

    out = _render(env, form, source_request=source)

    tags = re.findall(r'<span class="tag outl src-tag"([^>]*)>', out)
    fields = {
        re.search(r'data-wiz-src-field="(\w+)"', t).group(1): 'hidden' in t
        for t in tags
        if 'data-wiz-src-field' in t
    }
    assert fields == {
        'name': False,
        'game': False,
        'description': True,
        'ruleset': True,
        'start_time': True,
    }


def test_create_form_plain_create_shows_no_source_tags(env, ctx):
    out = _render(env, _make_form())

    assert 'data-wiz-src-tag' not in out
    assert 'data-wiz-source-line' not in out
    assert 'data-wiz-aside-source' not in out


def test_create_form_request_names_its_source_under_the_title(env, ctx):
    out = _render(env, _make_form(), source_request=_source_request())

    head = out[out.index('<h1') : out.index('lt-wiz-layout')]
    assert 'data-wiz-source-line' in head
    line = head[head.index('data-wiz-source-line') :]
    assert 'From ' in line
    assert 'Request #0142' in line
    assert 'Oma_Gerda' in line
    assert head.index('data-wiz-source-line') < head.index('lt-wiz-subline')


def test_create_form_aside_source_box_sits_between_party_and_status(env, ctx):
    out = _render(env, _make_form(), source_request=_source_request())

    aside = out[out.index('<aside') : out.index('</aside>')]
    assert (
        aside.index('Your timezone')
        < aside.index('data-wiz-aside-source')
        < aside.index('Will be created as')
    )
    box = aside[aside.index('data-wiz-aside-source') :]
    box = box[: box.index('Will be created as')]
    assert 'Prefill source' in box
    assert 'Accepted' in box
    assert 'Fields marked' in box
    assert 'notification color-info' in aside


_WIZARD_CSS = pathlib.Path(
    'byceps/static/style/lan_tournament_create_wizard.css'
).read_text()


def test_image_url_details_content_is_border_box():
    match = re.search(
        r'\.lt-wiz-urlalt::details-content\s*\{([^}]*)\}', _WIZARD_CSS
    )

    assert match is not None
    assert 'box-sizing: border-box' in match.group(1)


def test_image_row_keeps_no_gap_below_its_actions():
    match = re.search(
        r'\.lt-wiz-layout \.lt-wiz-img__actions:not\(:last-child\)\s*'
        r'\{([^}]*)\}',
        _WIZARD_CSS,
    )

    assert match is not None
    assert 'margin-bottom: 0' in match.group(1)


def test_nojs_players_per_team_legend_does_not_cut_into_the_box():
    # A rendered legend starts the fieldset's background at its midline;
    # a floated one does not, so the joined box stays white.
    scope = (
        r"\.lt-wiz-layout:not\(\.is-enhanced\) "
        r"\[data-wiz-scope='team-players'\]"
    )
    legend = re.search(scope + r' > legend\s*\{([^}]*)\}', _WIZARD_CSS)
    row = re.search(scope + r' > legend \+ \*\s*\{([^}]*)\}', _WIZARD_CSS)

    assert legend is not None
    assert 'float: left' in legend.group(1)
    assert 'width: 100%' in legend.group(1)
    assert row is not None
    assert 'clear: left' in row.group(1)


_IMAGE_JS = pathlib.Path(
    'byceps/static/behavior/lan_tournament_create_wizard_image.js'
).read_text()


def test_image_module_hides_the_drop_zone_while_a_row_shows():
    render = _IMAGE_JS[_IMAGE_JS.index('function renderLive()') :]
    render = render[: render.index('function onLiveAction()')]

    assert "var shown = status !== 'none';" in render
    assert 'setShown(live.root, shown);' in render
    assert 'setShown(dropEl, !shown);' in render


def test_image_module_parks_the_controls_while_uploading():
    place = _IMAGE_JS[_IMAGE_JS.index('function placeControls(') :]
    place = place[: place.index('function pickText(')]

    assert "status === 'uploading' ? 'park'" in place
    assert "host = key === 'row' ? live.actions : drop.actions" in place


# --- A refused request link, the server banner, start time, no-JS page ---


def _refused_request(**overrides):
    return _source_request(
        **(
            {
                'status': TournamentRequestStatus.withdrawn,
                'updated_at': datetime(2026, 10, 3, 8, 24),
                'decided_at': datetime(2026, 10, 2, 9, 0),
                'decided_by_id': 'u7',
            }
            | overrides
        )
    )


def _refusal(request=None, **overrides):
    arguments = {
        'proposer_name': 'Oma_Gerda',
        'decider_name': 'Orga_Olaf',
        'view_url': '/requests/r1',
    } | overrides
    return build_request_refusal(request or _refused_request(), **arguments)


def test_refusal_names_who_and_when_for_a_withdrawn_request(german_ctx):
    refusal = _refusal()

    assert refusal['lead'] == 'Request #0142 is no longer accepted.'
    assert refusal['detail'] == 'Oma_Gerda withdrew it at 10:24.'
    assert refusal['closing'] == (
        'No tournament was created; your entries are kept.'
    )
    assert refusal['viewUrl'] == '/requests/r1'
    assert refusal['statusLabel'] == 'Withdrawn'


def test_refusal_names_the_decider_of_a_rejected_request(german_ctx):
    request = _refused_request(status=TournamentRequestStatus.rejected)

    refusal = _refusal(request)

    assert refusal['detail'] == 'Orga_Olaf rejected it at 11:00.'
    assert refusal['statusLabel'] == 'Rejected'


def test_refusal_has_no_detail_when_nobody_acted_on_the_request(german_ctx):
    request = _refused_request(
        status=TournamentRequestStatus.tournament_created
    )

    refusal = _refusal(request)

    assert refusal['detail'] == ''
    assert refusal['statusLabel'] == 'Tournament created'


def test_refusal_carries_the_request_for_the_mapping_row(german_ctx):
    refusal = _refusal()

    assert refusal['request']['derivedContestantType'] == 'TEAM'
    assert refusal['request']['participantLimit'] == 12


def test_create_form_renders_the_refusal_once_with_two_actions(env, ctx):
    request = _refused_request()
    form = _make_form()
    form.from_request_id.errors = [
        (
            'Request #0142 is no longer accepted. No tournament was created; '
            'your entries are kept.'
        )
    ]

    out = _render(
        env,
        form,
        refusal={
            'lead': 'Request #0142 is no longer accepted.',
            'detail': 'Oma_Gerda withdrew it at 10:24.',
            'closing': 'No tournament was created; your entries are kept.',
            'viewUrl': '/requests/r1',
            'proposerName': 'Oma_Gerda',
            'statusLabel': 'Withdrawn',
            'request': {},
        },
        refused_request=request,
    )

    visible = out.replace(_island(out), '')
    box = visible[visible.index('data-wiz-refusal') :]
    box = box[: box.index('</div>\n          </div>')]
    assert visible.count('data-wiz-refusal') == 1
    assert visible.count('is no longer accepted') == 1
    assert '<strong>Request #0142 is no longer accepted.</strong>' in box
    assert 'Oma_Gerda withdrew it at 10:24.' in box
    assert 'No tournament was created; your entries are kept.' in box
    assert (
        '<button type="button" class="button is-compact" data-wiz-unlink' in box
    )
    assert 'Create without link to the request' in box
    assert 'href="/requests/r1"' in box
    assert 'View request' in box
    assert 'data-wiz-errsum' not in visible.replace('[data-wiz-errsum]', '')


def test_create_form_shows_the_refused_request_in_the_aside(env, ctx):
    out = _render(
        env,
        _make_form(),
        refusal={
            'lead': 'x',
            'detail': '',
            'closing': 'y',
            'viewUrl': '/r',
            'proposerName': 'Oma_Gerda',
            'statusLabel': 'Withdrawn',
            'request': {},
        },
        refused_request=_refused_request(),
    )

    box = out[out.index('data-wiz-aside-source') :]
    box = box[: box.index('</aside>')]
    assert 'Oma_Gerda' in box
    assert 'Withdrawn' in box
    assert 'Accepted' not in box
    # The provenance banner still claims no prefill.
    assert 'Prefilled from request' not in out


_USER_VIEW_LINK = 'href="/user_admin.view"'

_REFUSAL_STUB = {
    'lead': 'x',
    'detail': '',
    'closing': 'y',
    'viewUrl': '/r',
    'proposerName': 'Oma_Gerda',
    'statusLabel': 'Withdrawn',
    'request': {},
}


def _source_line(out):
    head = out[out.index('<h1') : out.index('lt-wiz-layout')]
    line = head[head.index('data-wiz-source-line') :]
    return line[: line.index('</div>')]


def _aside_source_box(out):
    box = out[out.index('data-wiz-aside-source') :]
    return box[: box.index('<div class="form-caption">')]


def test_create_form_links_the_proposer_for_a_user_viewer(env, ctx):
    out = _render(env, _make_form(), source_request=_source_request())

    assert f'<a {_USER_VIEW_LINK}>Oma_Gerda</a>' in _source_line(out)
    assert f'<a {_USER_VIEW_LINK}>Oma_Gerda</a>' in _aside_source_box(out)


def test_create_form_links_the_refused_proposer_for_a_user_viewer(env, ctx):
    out = _render(
        env,
        _make_form(),
        refusal=_REFUSAL_STUB,
        refused_request=_refused_request(),
    )

    assert f'<a {_USER_VIEW_LINK}>Oma_Gerda</a>' in _aside_source_box(out)


def test_create_form_shows_the_proposer_as_text_without_user_view(
    env, ctx, monkeypatch
):
    monkeypatch.setitem(
        env.globals,
        'g',
        _user_with_permissions({'lan_tournament.create'}),
    )

    out = _render(env, _make_form(), source_request=_source_request())

    assert _USER_VIEW_LINK not in out
    assert 'Oma_Gerda' in _source_line(out)
    assert '<a' not in _source_line(out).replace(
        '<a href="/view_request">Request #0142</a>', ''
    )
    box = _aside_source_box(out)
    assert 'Oma_Gerda' in box
    assert '<a href="/user' not in box
    assert 'Request #0142</a> by Oma_Gerda' in box


def test_create_form_shows_the_refused_proposer_as_text_without_user_view(
    env, ctx, monkeypatch
):
    monkeypatch.setitem(
        env.globals,
        'g',
        _user_with_permissions({'lan_tournament.create'}),
    )

    out = _render(
        env,
        _make_form(),
        refusal=_REFUSAL_STUB,
        refused_request=_refused_request(),
    )

    assert _USER_VIEW_LINK not in out
    assert 'Request #0142</a> by Oma_Gerda' in _aside_source_box(out)


def test_create_form_escapes_the_proposer_name_without_user_view(
    env, ctx, monkeypatch
):
    monkeypatch.setitem(env.globals, 'g', _user_with_permissions(set()))
    hostile = '<img src=x onerror=alert(1)>'

    out = _render(
        env,
        _make_form(),
        source_request=_source_request(),
        proposer=hostile,
    )

    assert '&lt;img src=x onerror=alert(1)&gt;' in _source_line(out)
    assert '&lt;img src=x onerror=alert(1)&gt;' in _aside_source_box(out)
    assert '<img src=x' not in out


def test_create_form_start_time_is_offered_in_the_browser_form(env, ctx):
    form = _make_form()
    form.start_time.data = datetime(2026, 10, 17, 14, 0)

    out = _render(env, form)

    field = re.search(r'<input[^>]*id="start_time"[^>]*>', out).group(0)
    assert 'value="2026-10-17T14:00"' in field
    assert '2026-10-17 14:00:00' not in out


def test_create_form_start_time_survives_a_failed_post(env, ctx):
    form = _make_form(
        {'name': '   ', 'start_time': '2026-10-17T14:00'}, validate=True
    )

    out = _render(env, form)

    field = re.search(r'<input[^>]*id="start_time"[^>]*>', out).group(0)
    assert 'value="2026-10-17T14:00"' in field


def test_create_form_shows_the_server_banner_for_a_field_error(env, ctx):
    out = _render(env, _make_form({'name': '   '}, validate=True))

    banner = re.search(
        r'<div[^>]*data-wiz-server-banner[^>]*>(.*?)</div>', out, re.DOTALL
    )
    assert banner is not None
    assert 'role="alert"' in banner.group(0)
    assert ' hidden' in banner.group(0)
    assert '<strong>The server rejected the creation.</strong>' in banner.group(
        1
    )
    assert 'We took you to the affected field.' in banner.group(1)
    assert out.index('data-wiz-server-banner') < out.index('data-wiz-notices')
    assert out.index('data-wiz-notices') < out.index('data-wiz-errsum')


def test_create_form_shows_no_server_banner_for_a_review_only_error(env, ctx):
    form = _make_form()
    form.form_errors.append('Something is wrong overall.')

    out = _render(env, form)

    assert 'data-wiz-server-banner' not in out.replace(
        '[data-wiz-server-banner]', ''
    )


def test_create_form_offers_the_intro_for_the_page_without_script(env, ctx):
    out = _render(env, _make_form())

    intro = re.search(
        r'<div[^>]*data-wiz-nojs-intro[^>]*>(.*?)</div>', out, re.DOTALL
    )
    assert intro is not None
    assert intro.group(1).startswith('Without JavaScript you see all settings')
    assert 'color-info' in intro.group(0)
    assert out.index('data-wiz-nojs-intro') < out.index('lt-wiz-layout"')


def test_create_form_numbers_the_scopes_for_the_page_without_script(env, ctx):
    out = _render(env, _make_form())

    for text in (
        '3 · Participants – only for Solo',
        '3 · Participants – only for Teams',
        '4 · Only for Highscore',
        '4 · Only for Free-for-All',
    ):
        assert text in out
    assert 'only 1v1' in out
    assert 'only for Highscore, automatic there' in out
    assert 'Comma-separated, place 1 first.' in out


def test_create_form_review_placeholder_is_the_short_draft_notice(env, ctx):
    out = _render(env, _make_form())

    review = out[out.index('data-wiz-review>') :]
    review = review[: review.index('</div>') + 6]
    assert (
        'The tournament is created as a draft. Registration stays closed.'
        in review
    )
    assert 'tournament page' not in review
