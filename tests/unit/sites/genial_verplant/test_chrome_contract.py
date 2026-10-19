"""
tests.unit.sites.genial_verplant.test_chrome_contract
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from collections import Counter
from datetime import datetime
from html.parser import HTMLParser
import re
from types import SimpleNamespace

from jinja2 import (
    ChoiceLoader,
    DictLoader,
    Environment,
    FileSystemLoader,
    StrictUndefined,
)
import pytest

from tests.unit.sites.genial_verplant._tokens import SITE_DIR, STYLE_DIR


ROOT = SITE_DIR.parents[1]
OVERRIDES_DIR = SITE_DIR / 'template_overrides'
CORE_COMMON = ROOT / 'byceps/services/core/blueprints/common/templates'
CORE_SITE = ROOT / 'byceps/services/core/blueprints/site/templates'
LOOK_JS = SITE_DIR / 'static' / 'behavior' / 'gv-look.js'

# The picker partial is included by the header, so these three cover the
# whole chrome the look engine binds to.
CHROME_TEMPLATES = (
    'layout/_header.html',
    'layout/_look_overlays.html',
    'layout/_footer.html',
)
JS_IDS = sorted(
    set(
        re.findall(
            r"getElementById\('([^']+)'\)", LOOK_JS.read_text(encoding='utf-8')
        )
    )
)

NOW = datetime(2031, 5, 4, 12, 30)
ANONYMOUS = SimpleNamespace(authenticated=False)
MEMBER = SimpleNamespace(
    authenticated=True,
    id='018f0000-0000-7000-8000-000000000001',
    as_user=lambda: SimpleNamespace(deleted=False, screen_name='Spock'),
)
NAV_ITEMS = [
    SimpleNamespace(target='/aktuell', label='Aktuell', current_page_id='news'),
    SimpleNamespace(
        target='/tickets', label='Tickets', current_page_id='tickets'
    ),
]
STUB_PAGE = (
    "{% extends 'layout/base.html' %}"
    '{% block body %}<p>Inhalt</p>{% endblock %}'
)
VOID_TAGS = {'input', 'img', 'br', 'hr', 'meta', 'link', 'source'}


class _Element:
    def __init__(self, tag, attrs, ancestors):
        self.tag = tag
        self.attrs = attrs
        self.ancestors = ancestors
        self.text = ''

    @property
    def classes(self):
        return (self.attrs.get('class') or '').split()

    def is_inside_id(self, element_id):
        return any(a.attrs.get('id') == element_id for a in self.ancestors)


class _Collector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.elements = []
        self._open = []

    def handle_starttag(self, tag, attrs):
        element = _Element(tag, dict(attrs), tuple(self._open))
        self.elements.append(element)
        if tag not in VOID_TAGS:
            self._open.append(element)

    def handle_startendtag(self, tag, attrs):
        self.elements.append(_Element(tag, dict(attrs), tuple(self._open)))

    def handle_endtag(self, tag):
        for index in range(len(self._open) - 1, -1, -1):
            if self._open[index].tag == tag:
                del self._open[index:]
                break

    def handle_data(self, data):
        for element in self._open:
            element.text += data


def _parse(html):
    collector = _Collector()
    collector.feed(html)
    collector.close()
    return collector.elements


def _make_g(user):
    return SimpleNamespace(
        user=user, party=None, current_locale=SimpleNamespace(language='de')
    )


@pytest.fixture(scope='module')
def env():
    environment = Environment(
        undefined=StrictUndefined,
        autoescape=True,
        loader=ChoiceLoader(
            [
                DictLoader({'stub_page.html': STUB_PAGE}),
                FileSystemLoader(
                    [str(OVERRIDES_DIR), str(CORE_COMMON), str(CORE_SITE)]
                ),
            ]
        ),
    )
    environment.globals.update(
        _=lambda text, **kwargs: text,
        url_for=lambda endpoint, **kwargs: f'/{endpoint}',
        url_for_site_file=lambda path, **kwargs: f'/site_file/{path}',
        get_nav_menu_items=lambda menu: NAV_ITEMS,
        get_flashed_messages=lambda **kwargs: [],
        request=SimpleNamespace(path='/aktuell'),
        g=_make_g(ANONYMOUS),
        now=NOW,
        datetime=datetime,
    )
    return environment


def _render(env, name, *, user=ANONYMOUS, now=NOW):
    return env.get_template(name).render(g=_make_g(user), now=now)


@pytest.fixture(scope='module')
def chrome(env):
    html = ''.join(_render(env, name) for name in CHROME_TEMPLATES)
    return _parse(html)


def test_js_id_extraction_finds_the_engine_contract():
    assert len(JS_IDS) >= 32


@pytest.mark.parametrize('js_id', JS_IDS)
def test_every_js_id_is_rendered(chrome, js_id):
    rendered_ids = {e.attrs['id'] for e in chrome if 'id' in e.attrs}

    assert js_id in rendered_ids


def test_rendered_chrome_ids_are_unique(chrome):
    counts = Counter(e.attrs['id'] for e in chrome if 'id' in e.attrs)

    assert {name: n for name, n in counts.items() if n > 1} == {}


def test_kampf_roster_has_three_slots_for_eras_1_to_3(chrome):
    slots = [e for e in chrome if 'fx-slot' in e.classes]

    assert len(slots) == 3
    assert {slot.attrs['data-v'] for slot in slots} == {'1', '2', '3'}
    for slot in slots:
        radios = [e for e in chrome if e.tag == 'input' and slot in e.ancestors]
        assert len(radios) == 1
        assert radios[0].attrs['type'] == 'radio'
        assert radios[0].attrs['name'] == 'era-f'
        assert radios[0].attrs['value'] == slot.attrs['data-v']


def test_scheme_radios_dark_and_light(chrome):
    radios = [
        e
        for e in chrome
        if e.tag == 'input' and e.attrs.get('name') == 'scheme'
    ]

    assert sorted(radio.attrs['value'] for radio in radios) == [
        'dark',
        'light',
    ]
    for radio in radios:
        assert radio.attrs['type'] == 'radio'
        assert radio.is_inside_id('schemes')
        assert radio.is_inside_id('look-panel')


@pytest.mark.parametrize('year', [2031, 2044])
def test_footer_year_egg_uses_current_year(env, year):
    elements = _parse(
        _render(env, 'layout/_footer.html', now=datetime(year, 1, 1))
    )

    eggs = [e for e in elements if e.attrs.get('id') == 'egg-year']
    assert len(eggs) == 1
    assert eggs[0].tag == 'button'
    assert eggs[0].text.strip() == str(year)
    retro_lines = [
        e
        for e in elements
        if 'small' in e.classes
        and any('retro-foot' in a.classes for a in e.ancestors)
    ]
    assert len(retro_lines) == 1
    assert f'1999–{year}' in retro_lines[0].text


def test_logged_in_header_has_post_logout_form(env):
    elements = _parse(_render(env, 'layout/_header.html', user=MEMBER))

    forms = [
        e
        for e in elements
        if e.tag == 'form'
        and e.attrs.get('action', '').endswith('authn_login.log_out')
    ]
    assert len(forms) == 1
    assert forms[0].attrs.get('method', '').lower() == 'post'
    buttons = [
        e for e in elements if e.tag == 'button' and forms[0] in e.ancestors
    ]
    assert [button.attrs.get('type') for button in buttons] == ['submit']
    get_links = [
        e
        for e in elements
        if e.tag == 'a' and 'log_out' in e.attrs.get('href', '')
    ]
    assert get_links == []


def test_anonymous_header_has_login_link_and_no_logout_form(env):
    elements = _parse(_render(env, 'layout/_header.html'))

    assert [e for e in elements if e.tag == 'form'] == []
    login_links = [
        e for e in elements if e.tag == 'a' and 'head-cta' in e.classes
    ]
    assert [link.attrs['href'] for link in login_links] == [
        '/authn_login.log_in_form'
    ]


def test_fresh_visit_markup_is_owly_dark(env):
    html = _render(env, 'stub_page.html')

    root = next(e for e in _parse(html) if e.tag == 'html')
    assert root.attrs['data-era'] == '1'
    assert root.attrs['data-theme'] == 'dark'


def test_pre_paint_script_adds_js_class_that_the_chrome_css_keys_on(env):
    html = _render(env, 'stub_page.html')

    head = html.partition('</head>')[0]
    pre_paint = re.search(r'<script>(.*?)</script>', head, re.DOTALL)
    assert pre_paint is not None
    assert "classList.add('js')" in pre_paint.group(1)
    chrome_css = (STYLE_DIR / 'gv-chrome.css').read_text(encoding='utf-8')
    assert 'html:not(.js) .look' in chrome_css
