"""
tests.unit.services.lan_tournament.test_public_orga_display
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

import pathlib

from jinja2 import DictLoader, Environment, StrictUndefined
import pytest

from byceps.services.lan_tournament.models.tournament_orga import (
    PublicTournamentOrga,
)
from byceps.services.user.models import User, UserID

from tests.helpers import generate_uuid


_TEMPLATE = pathlib.Path(
    'byceps/services/lan_tournament/blueprints/site/templates'
    '/site/lan_tournament/view.html'
)


@pytest.fixture(scope='module')
def env():
    src = _TEMPLATE.read_text()
    start = src.index('{%- if orgas %}')
    end = src.index('{%- endif %}\n  </div>', start) + len('{%- endif %}')

    e = Environment(
        undefined=StrictUndefined,
        autoescape=True,
        loader=DictLoader({'orga_block': src[start:end]}),
    )
    e.globals['_'] = lambda s, **kw: s
    return e


def _render(env, orgas):
    return env.get_template('orga_block').render(orgas=list(orgas))


def _make_user(*, screen_name='SomeUser') -> User:
    return User(
        id=UserID(generate_uuid()),
        screen_name=screen_name,
        initialized=True,
        suspended=False,
        deleted=False,
        avatar_url='https://example.test/avatar.png',
    )


def _make_orga(*, duties=None, screen_name='SomeUser'):
    return PublicTournamentOrga(
        user=_make_user(screen_name=screen_name),
        duties=duties,
    )


def test_public_orga_block_renders_when_orgas_exist(env):
    orgas = [
        _make_orga(screen_name='OrgaOne', duties='Bracket admin'),
        _make_orga(screen_name='OrgaTwo'),
    ]

    html = _render(env, orgas)

    assert 'OrgaOne' in html
    assert 'Bracket admin' in html
    assert 'OrgaTwo' in html


def test_public_orga_block_absent_when_no_orgas(env):
    html = _render(env, [])

    assert html.strip() == ''


def test_public_orga_block_handles_null_duties(env):
    """Print neither `None` nor empty parentheses for missing duties."""
    orgas = [_make_orga(duties=None, screen_name='NoDuties')]

    html = _render(env, orgas)

    assert 'NoDuties' in html
    assert 'None' not in html
    assert '(' not in html
