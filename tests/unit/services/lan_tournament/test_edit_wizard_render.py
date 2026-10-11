from datetime import datetime
from html.parser import HTMLParser
import json
import pathlib
import re
from typing import Any
from uuid import uuid4

from flask import Flask
from flask_babel import Babel, format_number
from jinja2 import DictLoader, Environment, StrictUndefined
from markupsafe import Markup
import pytest
from werkzeug.datastructures import MultiDict

from byceps.services.lan_tournament.blueprints.admin.forms import (
    TournamentCreateForm,
    TournamentUpdateForm,
)
from byceps.services.lan_tournament.lan_tournament_view_helpers import (
    build_create_wizard_context,
    edit_locked_fields,
    tournament_form_data,
)
from byceps.services.lan_tournament.models.contestant_type import (
    ContestantType,
)
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.playoff import PlayoffReleaseMode
from byceps.services.lan_tournament.models.tournament import (
    Tournament,
    TournamentID,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)

from tests.unit.services.lan_tournament import (
    test_create_wizard_render as render,
)


_TEMPLATES = pathlib.Path(
    'byceps/services/lan_tournament/blueprints/admin/templates'
    '/admin/lan_tournament'
)
_CREATE = _TEMPLATES / 'create_form.html'
_UPDATE = _TEMPLATES / 'update_form.html'

# The layout shows the main tab bar in `before_body`, as the real one does.
_LAYOUT_STUB = (
    '{% block head %}{% endblock %}'
    '<div id="before-body">{% block before_body %}'
    '<nav id="main-tabs"></nav>{% endblock %}</div>'
    '{% block body %}{% endblock %}'
    '{% block scripts %}{% endblock %}'
)
_SUBNAV_STUB = (
    '{% macro render_admin_subnav(tournament, active_tab) -%}'
    '<nav id="subnav" data-active="{{ active_tab }}"></nav>'
    '{%- endmacro %}'
)

_EDIT_URLS = {
    'create': '/update',
    'upload': '/upload',
    'delete_template': '/images/__ID__',
    'images': '/images',
    'validate': '/update/validate',
    'cancel': '/cancel',
}

_STATUS_LOCKED = frozenset({
    'name', 'game', 'category', 'start_time', 'contestant_type',
    'game_format', 'elimination_mode', 'score_ordering', 'min_players',
    'max_players', 'min_teams', 'max_teams', 'min_players_in_team',
    'max_players_in_team',
})  # fmt: skip
_FFA_LOCKED = frozenset({
    'point_table', 'group_size_min', 'group_size_max', 'advancement_count',
    'points_carry_to_losers',
})  # fmt: skip
_PLAYOFF_LOCKED = frozenset({
    'playoff_enabled', 'playoff_elimination_mode', 'playoff_group_count',
    'playoff_qualifiers_per_group', 'playoff_qualifier_count',
    'playoff_release_mode',
})  # fmt: skip

_PLAYOFFS = {
    'playoff_game_format': GameFormat.ONE_V_ONE,
    'playoff_elimination_mode': EliminationMode.SINGLE_ELIMINATION,
    'playoff_group_count': 3,
    'playoff_qualifiers_per_group': 2,
    'playoff_release_mode': PlayoffReleaseMode.MANUAL,
}
_ONGOING = {'tournament_status': TournamentStatus.ONGOING}

_LOCK_BANNER = (
    'This tournament is ongoing. Only description, image, and ruleset can be '
    'changed.'
)
_PLAYOFF_NOTE = 'The playoff settings stay editable until the release.'
_REMOVAL_NOTE = (
    'This format has no playoff phase. The playoff settings are removed when '
    'you save.'
)


@pytest.fixture(scope='module')
def app():
    a = Flask(__name__)
    a.config['TESTING'] = True
    a.config['LOCALE'] = 'en'
    a.config['BABEL_DEFAULT_LOCALE'] = 'en'
    a.config['BABEL_DEFAULT_TIMEZONE'] = 'Europe/Berlin'
    a.config['TIMEZONE'] = 'Europe/Berlin'
    Babel(a)
    return a


@pytest.fixture
def ctx(app):
    with app.app_context(), app.test_request_context('/'):
        yield


@pytest.fixture(scope='module')
def env():
    e = Environment(
        undefined=StrictUndefined,
        autoescape=True,
        loader=DictLoader(
            {
                'admin/lan_tournament/create_form.html': _CREATE.read_text(),
                'update_form.html': _UPDATE.read_text(),
                'admin/lan_tournament/_subnav.html': _SUBNAV_STUB,
                'layout/admin/lan_tournament.html': _LAYOUT_STUB,
                'macros/admin.html': render._ADMIN_MACROS_STUB,
                'macros/forms.html': render._FORMS_MACRO_SRC,
                'macros/icons.html': render._ICONS_STUB,
            }
        ),
    )
    e.globals['g'] = render._user_with_permissions({'user.view'})
    e.globals['_'] = lambda s, **kw: (Markup(s) % kw) if kw else Markup(s)  # noqa: S704
    e.globals['ngettext'] = lambda s, p, num, **kw: (s if num == 1 else p) % kw
    e.globals['url_for'] = render._url_for
    e.filters['dateformat'] = lambda dt, *a, **k: dt.strftime('%Y-%m-%d')
    e.filters['numberformat'] = format_number
    e.filters['timeformat'] = lambda dt, *a, **k: dt.strftime('%H:%M')
    return e


def _tournament(**overrides) -> Tournament:
    values: dict[str, Any] = {
        'id': TournamentID(uuid4()),
        'party_id': 'p1',
        'name': 'Rocket League Cup',
        'game': 'Rocket League',
        'description': 'Fast cars.',
        'image_url': None,
        'ruleset': 'Best of 3.',
        'start_time': None,
        'created_at': datetime(2026, 9, 1, 10, 0),
        'min_players': 2,
        'max_players': 16,
        'min_teams': None,
        'max_teams': None,
        'min_players_in_team': None,
        'max_players_in_team': None,
        'contestant_type': ContestantType.SOLO,
        'tournament_status': TournamentStatus.DRAFT,
        'game_format': GameFormat.ONE_V_ONE,
        'elimination_mode': EliminationMode.ROUND_ROBIN,
    }
    return Tournament(**(values | overrides))


def _update_form(tournament, formdata=None) -> TournamentUpdateForm:
    form = TournamentUpdateForm(
        formdata=formdata,
        data=None if formdata else tournament_form_data(tournament),
    )
    form.set_contestant_type_choices()
    form.set_game_format_choices()
    form.set_elimination_mode_choices()
    form.set_score_ordering_choices()
    return form


def _render(
    env,
    tournament,
    *,
    locked=None,
    staged_image=None,
    urls=_EDIT_URLS,
    form=None,
):
    party = render._party()
    form = form or _update_form(tournament)
    wizard = build_create_wizard_context(
        party,
        form,
        source_request=None,
        source_proposer_name=None,
        staged_image=staged_image,
        urls=urls,
        mode='edit',
        tournament=tournament,
        locked_fields=(
            edit_locked_fields(tournament)
            if locked is None
            else frozenset(locked)
        ),
        stored_values={},
    )
    return env.get_template('update_form.html').render(
        party=party,
        tournament=tournament,
        form=form,
        wizard=wizard,
        source_request=None,
        source_proposer_name=None,
        source_request_blocking_field_labels=[],
    )


def _render_create(env):
    party = render._party()
    form = TournamentCreateForm()
    form.set_contestant_type_choices()
    form.set_game_format_choices()
    form.set_elimination_mode_choices()
    form.set_score_ordering_choices()
    wizard = build_create_wizard_context(
        party,
        form,
        source_request=None,
        source_proposer_name=None,
        staged_image=None,
        urls=render._URLS,
    )
    return env.get_template('admin/lan_tournament/create_form.html').render(
        party=party,
        form=form,
        wizard=wizard,
        source_request=None,
        source_proposer_name=None,
        source_request_blocking_field_labels=[],
    )


def _tags(out, name):
    """Return the attributes of every `name` element."""

    class Collector(HTMLParser):
        def __init__(self):
            super().__init__()
            self.found = []

        def handle_starttag(self, tag, attrs):
            if tag == name:
                self.found.append(dict(attrs))

    collector = Collector()
    collector.feed(out)
    return collector.found


def _disabled_names(out):
    return {
        attrs['name']
        for tag in ('input', 'select', 'textarea')
        for attrs in _tags(out, tag)
        if 'disabled' in attrs
    }


def _visible(out):
    """Return the page without the config island, which serves every string."""
    return out.replace(render._island(out), '')


def _section(out, start, end):
    return out[out.index(start) : out.index(end, out.index(start))]


def test_update_form_extends_the_wizard(env, ctx):
    out = _render(env, _tournament())

    assert re.findall(r'data-wiz-step="(\d)"', out) == list('012345')
    assert (
        len(
            re.findall(r'<fieldset class="lt-wiz-step[^"]*" data-wiz-step', out)
        )
        == 5
    )
    assert out.count('<section class="lt-wiz-step box" data-wiz-step="5">') == 1
    form_tag = re.search(r'<form [^>]*>', out).group(0)
    assert 'data-lt-wizard' in form_tag
    assert 'data-wiz-mode="edit"' in form_tag
    config = json.loads(render._island(out))
    assert config['mode'] == 'edit'
    assert config['sessionKey'] is None
    assert '<h1 class="title">Edit tournament</h1>' in out
    assert '6 · Review and save' in out
    assert 'Review and create' not in _visible(out)


def test_update_form_keeps_the_main_tabs_and_adds_backlink_and_subnav(env, ctx):
    tournament = _tournament()

    out = _render(env, tournament)

    before_body = out[: out.index('<h1')]
    assert (
        before_body.index('id="main-tabs"')
        < before_body.index(
            '<a class="backlink" href="/view">Rocket League Cup</a>'
        )
        < before_body.index('<nav id="subnav" data-active="">')
    )


def test_edit_form_posts_to_update_and_has_only_the_image_hidden_field(
    env, ctx
):
    out = _render(env, _tournament(image_id=uuid4()))

    forms = _tags(out, 'form')
    assert len(forms) == 1
    assert forms[0]['action'] == '/update'
    assert forms[0]['method'] == 'post'
    assert forms[0]['enctype'] == 'multipart/form-data'
    hidden = [
        attrs['name']
        for attrs in _tags(out, 'input')
        if attrs.get('type') == 'hidden'
    ]
    assert hidden == ['image_id']
    assert 'action="/create"' not in out
    assert 'from_request_id' not in out
    assert 'submission_token' not in out


def test_edit_form_has_exactly_one_submit_button_labelled_save_changes(
    env, ctx
):
    out = _render(env, _tournament())

    submits = [a for a in _tags(out, 'button') if a.get('type') == 'submit']
    assert len(submits) == 1
    assert 'data-wiz-submit' in submits[0]
    assert re.search(
        r'<button type="submit"[^>]*data-wiz-submit>Save changes</button>', out
    )
    assert 'Create draft tournament' not in _visible(out)


@pytest.mark.parametrize(
    ('urls', 'cancel'),
    [(_EDIT_URLS, '/cancel'), ({'create': '/update'}, '/view')],
    ids=['configured', 'falls-back-to-the-tournament'],
)
def test_edit_form_cancel_leaves_for_the_tournament(env, ctx, urls, cancel):
    out = _render(env, _tournament(), urls=urls)

    (link,) = [a for a in _tags(out, 'a') if 'data-wiz-cancel' in a]
    assert link['href'] == cancel


# fmt: off
@pytest.mark.parametrize(
    'locked',
    [
        pytest.param(frozenset(), id='nothing'),
        pytest.param(frozenset({'name'}), id='a-text-field'),
        pytest.param(frozenset({'start_time'}), id='the-start-time'),
        pytest.param(
            frozenset({'contestant_type', 'elimination_mode'}), id='cards'
        ),
        pytest.param(_STATUS_LOCKED, id='status'),
        pytest.param(_FFA_LOCKED, id='free-for-all'),
        pytest.param(_PLAYOFF_LOCKED, id='playoffs'),
        pytest.param(
            _STATUS_LOCKED | _FFA_LOCKED | _PLAYOFF_LOCKED, id='everything'
        ),
    ],
)
# fmt: on
def test_edit_form_disables_locked_fields(env, ctx, locked):
    out = _render(env, _tournament(), locked=locked)

    assert _disabled_names(out) == locked
    assert len(re.findall(r'class="form-caption dimmed lt-wiz-lock"', out)) == len(
        locked
    )
    assert ('data-locked' in out) is ('point_table' in locked)
    assert ('data-ffa-add' in out) is ('point_table' not in locked)


def test_edit_form_never_disables_the_cosmetic_fields(env, ctx):
    everything = _STATUS_LOCKED | _FFA_LOCKED | _PLAYOFF_LOCKED

    out = _render(env, _tournament(), locked=everything)

    names = {
        attrs['name']
        for tag in ('input', 'select', 'textarea')
        for attrs in _tags(out, tag)
        if 'name' in attrs
    }
    assert {'description', 'ruleset', 'image', 'image_alt_text'} <= names
    assert not {'description', 'ruleset', 'image', 'image_alt_text', 'image_url'} & (
        _disabled_names(out)
    )


def test_edit_form_points_a_locked_control_at_its_caption(env, ctx):
    out = _render(env, _tournament(), locked={'name', 'category'})

    name = next(a for a in _tags(out, 'input') if a.get('name') == 'name')
    category = next(a for a in _tags(out, 'select') if a.get('name') == 'category')
    assert name['aria-describedby'] == 'name-cap name-lock'
    assert category['aria-describedby'] == 'category-lock'
    assert 'id="name-lock"' in out
    assert 'id="category-lock"' in out


# fmt: off
@pytest.mark.parametrize(
    'overrides',
    [
        pytest.param({}, id='draft'),
        pytest.param(_ONGOING, id='ongoing'),
        pytest.param({'tournament_status': TournamentStatus.PAUSED}, id='paused'),
        pytest.param(
            {'tournament_status': TournamentStatus.COMPLETED, **_PLAYOFFS,
             'playoff_released_at': datetime(2026, 9, 2)},
            id='completed-with-released-playoffs',
        ),
        pytest.param({**_ONGOING, **_PLAYOFFS}, id='ongoing-with-playoffs'),
    ],
)
# fmt: on
def test_edit_form_disables_what_the_edit_rules_lock(env, ctx, overrides):
    tournament = _tournament(**overrides)

    out = _render(env, tournament)

    assert _disabled_names(out) == edit_locked_fields(tournament)


# fmt: off
@pytest.mark.parametrize(
    ('overrides', 'banner', 'playoff_note'),
    [
        pytest.param({}, False, False, id='draft'),
        pytest.param(_ONGOING, True, False, id='ongoing'),
        pytest.param(
            {'tournament_status': TournamentStatus.PAUSED}, True, False,
            id='paused',
        ),
        pytest.param({**_ONGOING, **_PLAYOFFS}, True, True, id='ongoing-with-playoffs'),
        pytest.param(
            {**_ONGOING, **_PLAYOFFS, 'playoff_released_at': datetime(2026, 9, 2)},
            True, False, id='ongoing-with-released-playoffs',
        ),
        pytest.param(
            {'tournament_status': TournamentStatus.COMPLETED}, False, False,
            id='completed',
        ),
    ],
)
# fmt: on
def test_edit_form_shows_the_lock_banner_when_ongoing(
    env, ctx, overrides, banner, playoff_note
):
    out = _render(env, _tournament(**overrides))

    assert ('data-wiz-lock-banner' in out) is banner
    assert (_LOCK_BANNER in out) is banner
    assert (_PLAYOFF_NOTE in out) is playoff_note
    if banner:
        assert (
            out.index('data-wiz-notices')
            < out.index('data-wiz-lock-banner')
            < out.index('<form ')
        )


def test_edit_form_aside_shows_status_not_will_be_created(env, ctx):
    out = _render(env, _tournament(**_ONGOING))

    aside = _section(out, '<aside class="lt-wiz-aside">', '</aside>')
    assert 'Will be created as' not in aside
    assert 'Registration closed' not in aside
    assert '<div class="data-label">Status</div>' in aside
    assert '<span class="tag color-success">Ongoing</span>' in aside
    assert 'Nothing is saved until the last step.' not in aside
    assert 'Changes are saved only when you press Save changes.' in aside
    assert 'Party 36' in aside
    assert 'Prefill source' not in aside


def test_edit_form_head_prefixes_the_status_tag(env, ctx):
    out = _render(env, _tournament(tournament_status=TournamentStatus.PAUSED))

    subline = _section(out, 'class="lt-wiz-subline">', '</div>')
    assert subline.startswith(
        'class="lt-wiz-subline"><span class="tag color-warning">Paused</span> '
        'Party 36 · 120 seats · Europe/Berlin'
    )


def test_edit_form_shows_the_stored_image_as_staged(env, ctx):
    image = render._staged_image('stored.png')
    tournament = _tournament(image_id=image.id, image_alt_text='A logo')

    out = _render(env, tournament, staged_image=image)

    staged = _section(out, 'data-wiz-image-staged', 'data-wiz-drop')
    assert f'src="/data/parties/p1/lan_tournament/images/{image.id}.png"' in staged
    assert '<strong>stored.png</strong>' in staged
    image_id = next(
        a for a in _tags(out, 'input') if a.get('name') == 'image_id'
    )
    assert image_id['value'] == str(image.id)
    image_url = next(
        a for a in _tags(out, 'input') if a.get('name') == 'image_url'
    )
    assert image_url.get('value', '') == ''
    assert '<details class="lt-wiz-urlalt">' in out
    alt = next(
        a for a in _tags(out, 'input') if a.get('name') == 'image_alt_text'
    )
    assert alt['value'] == 'A logo'
    assert json.loads(render._island(out))['stagedImage']['imageId'] == str(
        image.id
    )


def test_edit_form_shows_the_stored_image_url_without_an_uploaded_image(
    env, ctx
):
    tournament = _tournament(image_url='https://example.org/banner.png')

    out = _render(env, tournament)

    assert 'data-wiz-image-staged' not in out
    assert '<details class="lt-wiz-urlalt" open>' in out
    image_url = next(
        a for a in _tags(out, 'input') if a.get('name') == 'image_url'
    )
    assert image_url['value'] == 'https://example.org/banner.png'
    image_id = next(
        a for a in _tags(out, 'input') if a.get('name') == 'image_id'
    )
    assert image_id.get('value', '') == ''


def test_edit_form_has_a_hidden_playoff_removal_note_in_step_four(env, ctx):
    out = _render(env, _tournament())

    step = _section(out, 'data-wiz-step="4"', 'data-wiz-step="5"')
    (note,) = [a for a in _tags(step, 'div') if 'data-wiz-playoff-removal' in a]
    assert 'hidden' in note
    assert f'>{_REMOVAL_NOTE}</div>' in step
    assert out.count('data-wiz-playoff-removal') == 1


def test_edit_form_review_and_banner_speak_of_changes(env, ctx):
    tournament = _tournament()
    form = _update_form(
        tournament,
        MultiDict({'category': 'MAIN', 'name': '   ', 'min_players': 'abc'}),
    )
    form.validate()
    form.form_errors.append('Something is wrong overall.')

    out = _render(env, tournament, form=form)

    assert '<strong>The server rejected the changes.</strong>' in out
    assert 'The server rejected the creation.' not in _visible(out)
    review = out[out.index('data-wiz-step="5"') :]
    assert 'Something is wrong overall.' in review
    assert 'Changes are saved only when you press Save changes.' in review
    assert 'The tournament is created as a draft.' not in _visible(out)
    assert '2 entries need attention' in out


def test_edit_form_keeps_the_create_slice_anchors():
    src = _CREATE.read_text()
    form_open = "<form action=\"{{ url_for('.create', party_id=party.id) }}\""

    for anchor in (
        '{% block before_body %}',
        form_open,
        '{{ form.from_request_id() }}',
        '{# Provenance banner',
        '{# /Provenance banner #}',
    ):
        assert src.count(anchor) == 1, anchor
    assert re.search(
        re.escape(form_open) + r'[^\n]*>\n\s*\{\{ form\.from_request_id\(\) \}\}',
        src,
    )
    update = _UPDATE.read_text()
    assert re.findall(r'\{%-? block (\w+)', update) == ['before_body']
    assert update.startswith(
        "{% extends 'admin/lan_tournament/create_form.html' %}"
    )


def test_edit_form_has_no_unrendered_placeholder(env, ctx):
    out = _render(env, _tournament(**_ONGOING, **_PLAYOFFS))

    visible = re.sub(r'data-label-\w+="[^"]*"', '', _visible(out))
    assert '%(' not in visible


def test_create_form_carries_no_edit_markup(env, ctx):
    out = _visible(_render_create(env))

    for marker in (
        'data-wiz-mode',
        'data-wiz-lock-banner',
        'data-wiz-playoff-removal',
        'lt-wiz-lock',
        'Save changes',
        'Review and save',
        'Edit tournament',
        'The server rejected the changes.',
    ):
        assert marker not in out, marker
    assert 'Create draft tournament' in out
    assert 'Will be created as' in out
    assert re.search(r'<form [^>]*action="/create"', out)
