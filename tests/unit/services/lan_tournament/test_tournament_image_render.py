"""
tests.unit.services.lan_tournament.test_tournament_image_render
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from pathlib import Path
import re
from types import SimpleNamespace

from jinja2 import DictLoader, Environment, StrictUndefined
import pytest


_ROOT = Path(__file__).resolve().parents[4]
_BLUEPRINTS = _ROOT / 'byceps/services/lan_tournament/blueprints'

_SITE_INDEX = _BLUEPRINTS / 'site/templates/site/lan_tournament/index.html'
_SITE_VIEW = _BLUEPRINTS / 'site/templates/site/lan_tournament/view.html'
_BOTE_INDEX = (
    _ROOT
    / 'sites/totalverplant-36/template_overrides/site/lan_tournament/index.html'
)
_ADMIN_VIEW = _BLUEPRINTS / 'admin/templates/admin/lan_tournament/view.html'

_IMAGE_BLOCK = re.compile(
    r'\{%-? if tournament\.image_url %\}.*?\{%-? endif %\}', re.DOTALL
)


def _render_image_block(path, tournament):
    """Render the `image_url` conditional of the real template."""
    source = path.read_text(encoding='utf-8')
    match = _IMAGE_BLOCK.search(source)
    assert match is not None, path
    env = Environment(
        undefined=StrictUndefined,
        autoescape=True,
        loader=DictLoader({'block': match.group(0)}),
    )
    env.globals['render_icon'] = lambda name: f'[icon:{name}]'
    return env.get_template('block').render(tournament=tournament)


def _tournament(image_url='/img/t.webp', image_alt_text=None):
    return SimpleNamespace(image_url=image_url, image_alt_text=image_alt_text)


@pytest.mark.parametrize(
    'path',
    [_SITE_INDEX, _SITE_VIEW, _BOTE_INDEX],
    ids=['index', 'view', 'bote'],
)
def test_site_templates_render_alt_text(path):
    html = _render_image_block(
        path, _tournament(image_alt_text='Bracket "finals" <b>')
    )

    assert 'alt="Bracket &#34;finals&#34; &lt;b&gt;"' in html


def test_site_index_renders_alt_text():
    html = _render_image_block(_SITE_INDEX, _tournament(image_alt_text='Logo'))

    assert '<img src="/img/t.webp" alt="Logo">' in html


def test_site_view_renders_alt_text():
    html = _render_image_block(_SITE_VIEW, _tournament(image_alt_text='Logo'))

    assert '<img src="/img/t.webp" alt="Logo">' in html


def test_bote_override_index_renders_alt_text():
    html = _render_image_block(_BOTE_INDEX, _tournament(image_alt_text='Logo'))

    assert '<img src="/img/t.webp" alt="Logo">' in html


@pytest.mark.parametrize('path', [_SITE_INDEX, _SITE_VIEW, _BOTE_INDEX])
def test_site_templates_render_empty_alt_when_decorative(path):
    html = _render_image_block(path, _tournament(image_alt_text=None))

    assert '<img src="/img/t.webp" alt="">' in html


def test_admin_view_renders_image_block_only_when_image_url():
    with_image = _render_image_block(
        _ADMIN_VIEW, _tournament(image_alt_text='Logo')
    )
    without_image = _render_image_block(
        _ADMIN_VIEW, _tournament(image_url=None)
    )

    assert (
        '<img class="lt-admin-tournament-image" src="/img/t.webp" alt="Logo">'
        in with_image
    )
    assert '<img' not in without_image
