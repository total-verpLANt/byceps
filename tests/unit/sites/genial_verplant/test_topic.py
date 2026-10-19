"""
tests.unit.sites.genial_verplant.test_topic
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

import colorsys
from datetime import datetime
from functools import cache
from html.parser import HTMLParser
from types import SimpleNamespace

from jinja2 import (
    ChoiceLoader,
    DictLoader,
    Environment,
    FileSystemLoader,
    StrictUndefined,
)
import pytest

from tests.unit.sites.genial_verplant._tokens import (
    contrast,
    hex_to_rgb,
    load_token_sets,
    SITE_DIR,
)


ROOT = SITE_DIR.parents[1]
OVERRIDES_DIR = SITE_DIR / 'template_overrides'
STATIC_DIR = SITE_DIR / 'static'
CORE_COMMON = ROOT / 'byceps/services/core/blueprints/common/templates'
CORE_SITE = ROOT / 'byceps/services/core/blueprints/site/templates'

TOKEN_SETS = load_token_sets()

# The topic data partial ships empty, so this fixture (in the documented
# entry shape) is the only entry the checks are sure to see.
RENTE_FIXTURE = """\
{% set TOPICS = [
  {
    'party_id': 'gv-37',
    'name': 'Rente',
    'pun': ['ver', 'RENT', 'et'],
    'colour': '#a78bfa',
    'on_dark': '#c4b5fd',
    'on_light': '#6d28d9',
    'glow': 'rgba(167, 139, 250, .38)',
    'art': 'topics/test.svg.html',
    'teaser_at': (2027, 6, 1, 8, 0),
    'announce_at': (2027, 8, 1, 16, 0),
    'teaser_text': none,
    'footer_line': none,
    'error_line': none,
    'og_image': none,
  },
] %}
"""
EMPTY_FIXTURE = '{% set TOPICS = [] %}'

ART_MARKER = 'M34 168 Q112 198 190 168'
ART_RENTE = ''.join(
    [
        '<svg viewBox="0 0 240 200" aria-hidden="true">',
        '<ellipse cx="112" cy="186" rx="86" ry="7" fill="var(--tp-shadow)"/>',
        f'<path d="{ART_MARKER}" fill="none" stroke="var(--tp-ink)"',
        ' stroke-width="7" stroke-linecap="round"/>',
        '<rect x="64" y="118" width="96" height="16" rx="6" fill="var(--tp)"',
        ' stroke="var(--tp-ink)" stroke-width="5"/>',
        '<rect x="186" y="148" width="30" height="32" rx="5"',
        ' fill="var(--tp-acc)" stroke="var(--tp-ink)" stroke-width="4"/>',
        '</svg>',
    ]
)
STUB_BASE = '{% block body %}{% endblock %}'

REQUIRED_KEYS = {
    'party_id',
    'name',
    'pun',
    'colour',
    'on_dark',
    'on_light',
    'glow',
    'art',
    'teaser_at',
    'announce_at',
}
OPTIONAL_KEYS = {'teaser_text', 'footer_line', 'error_line', 'og_image'}


def _parse_topics(source):
    """Return the `TOPICS` list a data partial source defines."""
    environment = Environment(autoescape=True)
    return list(environment.from_string(source).module.TOPICS)


RENTE = _parse_topics(RENTE_FIXTURE)[0]
MINIMAL = {k: v for k, v in RENTE.items() if k not in OPTIONAL_KEYS}
OPTIONALS_SET = {
    **RENTE,
    'teaser_text': 'Haltet die Augen offen.',
    'footer_line': 'Diesmal wird alles anders.',
    'error_line': 'Hier gibt es nichts zu sehen.',
    'og_image': 'style/og/9c1e.png',
}


def _source_for(entry):
    return '{% set TOPICS = [' + repr(entry) + '] %}'


MINIMAL_FIXTURE = _source_for(MINIMAL)
OPTIONALS_FIXTURE = _source_for(OPTIONALS_SET)

SHIPPED_TOPICS = _parse_topics(
    (OVERRIDES_DIR / 'layout' / '_topics.html').read_text(encoding='utf-8')
)
ENTRIES = [
    pytest.param(RENTE, id='fixture-' + RENTE['party_id']),
    *(pytest.param(entry, id=entry['party_id']) for entry in SHIPPED_TOPICS),
]

PARTY = SimpleNamespace(
    id='gv-37',
    title='genial verpLANt #37',
    starts_at=datetime(2027, 9, 17, 16, 0),
    ends_at=datetime(2027, 9, 19, 12, 0),
)
ANONYMOUS = SimpleNamespace(authenticated=False)

BEFORE_TEASER = datetime(2027, 5, 31, 23, 59)
TEASER = datetime(2027, 7, 1, 12, 0)
REVEALED = datetime(2027, 8, 20, 12, 0)
AFTER_END = datetime(2027, 9, 20, 12, 0)
ALL_STATE_TIMES = [BEFORE_TEASER, TEASER, REVEALED, AFTER_END]
STATE_TIME_IDS = ['before-teaser', 'teaser', 'revealed', 'after-party']

RENDERED = (
    'layout/_header.html',
    'layout/_footer.html',
    'layout/_look_overlays.html',
    'layout/_topic_tokens.html',
    'site/homepage/_hero.html',
    'error/not_found.html',
)
TEASER_LINE = 'Das Thema wird am 1.8. verraten.'
TOPIC_LINE = 'Thema der #37: <span class="tp-label">Rente</span>'
BRAND_LABEL = 'genial verpLANt, Startseite'
LEAK_TOKENS = (
    'Rente',
    'RENT',
    '#a78bfa',
    '#c4b5fd',
    '#6d28d9',
    'rgba(167, 139, 250',
    '--tp:',
    ART_MARKER,
)

DARK_LOOKS = ('owly-dark', 'clan-2003', 'lan-99')
SURFACES = ('--bg', '--s1', '--s2')
# Background of the LAN '99 header bar, where the pun takes `on_light`.
LAN_99_HEADER = '#c0c0c0'
BRAND_ORANGE = '#f39200'
# `len('genial ')` of the 18-character wordmark budget goes to the fixed part.
PUN_BUDGET = 18 - len('genial ')


class _Element:
    def __init__(self, tag, attrs, ancestors):
        self.tag = tag
        self.attrs = attrs
        self.ancestors = ancestors
        self.text = ''

    @property
    def classes(self):
        return (self.attrs.get('class') or '').split()

    def is_inside(self, css_class):
        return any(css_class in a.classes for a in self.ancestors)


class _Collector(HTMLParser):
    VOID_TAGS = {'input', 'img', 'br', 'hr', 'meta', 'link', 'source'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.elements = []
        self._open = []

    def handle_starttag(self, tag, attrs):
        element = _Element(tag, dict(attrs), tuple(self._open))
        self.elements.append(element)
        if tag not in self.VOID_TAGS:
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


def _select(html, tag=None, css_class=None):
    collector = _Collector()
    collector.feed(html)
    collector.close()
    return [
        element
        for element in collector.elements
        if (tag is None or element.tag == tag)
        and (css_class is None or css_class in element.classes)
    ]


def _text(element):
    return ' '.join(element.text.split())


@cache
def _env(topics_source):
    environment = Environment(
        undefined=StrictUndefined,
        autoescape=True,
        loader=ChoiceLoader(
            [
                DictLoader(
                    {
                        'layout/_topics.html': topics_source,
                        'topics/test.svg.html': ART_RENTE,
                        'layout/base_auto.html': STUB_BASE,
                    }
                ),
                FileSystemLoader(
                    [str(OVERRIDES_DIR), str(CORE_COMMON), str(CORE_SITE)]
                ),
            ]
        ),
    )
    environment.filters['datetimeformat'] = lambda value, pattern: (
        value.strftime('%d.%m.%Y %H:%M')
    )
    environment.globals.update(
        _=lambda text, **kwargs: text,
        url_for=lambda endpoint, **kwargs: f'/{endpoint}',
        url_for_site_file=lambda path, **kwargs: f'/site_file/{path}',
        get_nav_menu_items=lambda menu: [],
        datetime=datetime,
    )
    return environment


def _make_g(party):
    return SimpleNamespace(user=ANONYMOUS, party=party)


def _resolve(source, now, party=PARTY):
    """Return the resolver module as the templates see it."""
    template = _env(source).get_template('layout/_topic.html')
    return template.make_module({'g': _make_g(party), 'now': now})


def _pages(source, now, party=PARTY):
    """Render every template that carries a topic slot."""
    environment = _env(source)
    return {
        name: environment.get_template(name).render(
            g=_make_g(party), now=now, total=100, available=40
        )
        for name in RENDERED
    }


@pytest.fixture(scope='module')
def neutral():
    return _pages(RENTE_FIXTURE, BEFORE_TEASER)


@pytest.fixture(scope='module')
def teaser():
    return _pages(RENTE_FIXTURE, TEASER)


@pytest.fixture(scope='module')
def revealed():
    return _pages(RENTE_FIXTURE, REVEALED)


def _leaks(pages):
    html = ''.join(pages.values())
    return [token for token in LEAK_TOKENS if token in html]


def _hue(colour):
    return colorsys.rgb_to_hsv(*(c / 255 for c in hex_to_rgb(colour)))[0] * 360


def _hue_distance(first, second):
    """Return the angle between two colours on the hue wheel, 0 to 180."""
    distance = abs(_hue(first) - _hue(second))
    return min(distance, 360 - distance)


def _with_lightness(colour, lightness):
    hue, _, saturation = colorsys.rgb_to_hls(
        *(c / 255 for c in hex_to_rgb(colour))
    )
    red, green, blue = colorsys.hls_to_rgb(hue, lightness, saturation)
    return '#{:02x}{:02x}{:02x}'.format(
        *(round(c * 255) for c in (red, green, blue))
    )


def _nearest_passing_lightness(colour, requirements):
    """Return `(HSL lightness in %, hex)` closest to `colour` that passes.

    `requirements` is a list of `(label, background, minimum ratio)`.
    """
    current = colorsys.rgb_to_hls(*(c / 255 for c in hex_to_rgb(colour)))[1]
    for step in sorted(range(1001), key=lambda s: abs(s / 1000 - current)):
        candidate = _with_lightness(colour, step / 1000)
        if all(
            contrast(candidate, background) >= minimum
            for _, background, minimum in requirements
        ):
            return step / 10, candidate
    return None


def _contrast_failures(colour, requirements):
    failures = []
    for label, background, minimum in requirements:
        ratio = contrast(colour, background)
        if ratio < minimum:
            failures.append(
                f'{label} {background}: {ratio:.2f}:1 (needs {minimum}:1)'
            )
    return failures


def _contrast_message(entry, key, requirements):
    colour = entry[key]
    failures = _contrast_failures(colour, requirements)
    if not failures:
        return ''
    nearest = _nearest_passing_lightness(colour, requirements)
    hint = (
        f'nearest passing HSL lightness is {nearest[0]}% ({nearest[1]})'
        if nearest
        else 'no lightness passes; change the hue or saturation'
    )
    return (
        f'{entry["party_id"]} {key} {colour} fails: '
        + '; '.join(failures)
        + f'. Hint: {hint}.'
    )


def _dark_requirements():
    return [
        (f'{look} {surface}', TOKEN_SETS[look][surface], 4.5)
        for look in DARK_LOOKS
        for surface in SURFACES
    ]


def _light_requirements():
    return [
        (f'owly-light {surface}', TOKEN_SETS['owly-light'][surface], 4.5)
        for surface in SURFACES
    ] + [('lan-99 header', LAN_99_HEADER, 3.0)]


# ---------------------------------------------------------------- states


def test_topic_entries_parametrization_is_never_empty():
    # The shipped list may be empty; the fixture keeps every check below
    # from passing over an empty parameter set.
    assert isinstance(SHIPPED_TOPICS, list)
    assert len(ENTRIES) == 1 + len(SHIPPED_TOPICS)
    assert ENTRIES[0].values[0]['name'] == 'Rente'


@pytest.mark.parametrize('now', ALL_STATE_TIMES, ids=STATE_TIME_IDS)
def test_topic_state_neutral_without_entry(now):
    resolved = _resolve(EMPTY_FIXTURE, now)

    assert resolved.state == 'none'
    assert resolved.topic is None
    assert resolved.teaser is None


# fmt: off
@pytest.mark.parametrize(
    ('now', 'state'),
    [
        pytest.param(datetime(2027, 5, 31, 23, 59), 'none',   id='before-teaser'),
        pytest.param(datetime(2027, 6,  1,  8,  0), 'teaser', id='teaser-boundary'),
        pytest.param(datetime(2027, 7,  1, 12,  0), 'teaser', id='teaser'),
        pytest.param(datetime(2027, 8,  1, 15, 59), 'teaser', id='before-announce'),
        pytest.param(datetime(2027, 8,  1, 16,  0), 'on',     id='announce-boundary'),
        pytest.param(datetime(2027, 8, 20, 12,  0), 'on',     id='revealed'),
        pytest.param(datetime(2027, 9, 19, 11, 59), 'on',     id='last-party-minute'),
        pytest.param(datetime(2027, 9, 19, 12,  0), 'none',   id='party-end-boundary'),
        pytest.param(datetime(2027, 9, 20, 12,  0), 'none',   id='after-party'),
    ],
)
# fmt: on
def test_topic_state_by_time(now, state):
    assert _resolve(RENTE_FIXTURE, now).state == state


def test_topic_state_neutral_for_other_party():
    other = SimpleNamespace(
        id='gv-38',
        title='genial verpLANt #38',
        starts_at=PARTY.starts_at,
        ends_at=PARTY.ends_at,
    )

    resolved = _resolve(RENTE_FIXTURE, REVEALED, party=other)

    assert resolved.state == 'none'
    assert resolved.topic is None


def test_topic_state_neutral_without_party():
    resolved = _resolve(RENTE_FIXTURE, REVEALED, party=None)

    assert resolved.state == 'none'
    assert resolved.num == ''


def test_resolver_exports_the_entry_only_in_its_own_state():
    teaser = _resolve(RENTE_FIXTURE, TEASER)
    revealed = _resolve(RENTE_FIXTURE, REVEALED)

    assert teaser.topic is None
    assert set(teaser.teaser) == {'announce_label', 'text'}
    assert teaser.teaser['announce_label'] == '1.8.'
    assert revealed.teaser is None
    assert revealed.topic['name'] == 'Rente'


# fmt: off
@pytest.mark.parametrize(
    ('title', 'num'),
    [
        ('genial verpLANt #37',   '37'),
        ('genial verpLANt # 42 ', '42'),
        ('genial verpLANt',       ''),
        ('genial verpLANt #',     ''),
        ('genial verpLANt #3a',   ''),
        ('genial verpLANt #²',    ''),
        ('genial verpLANt #٣',    ''),
    ],
)
# fmt: on
def test_party_number_is_ascii_digits_or_empty(title, num):
    party = SimpleNamespace(
        id='gv-37',
        title=title,
        starts_at=PARTY.starts_at,
        ends_at=PARTY.ends_at,
    )

    assert _resolve(RENTE_FIXTURE, REVEALED, party=party).num == num


# ---------------------------------------------------------------- render


@pytest.mark.parametrize('now', ALL_STATE_TIMES, ids=STATE_TIME_IDS)
@pytest.mark.parametrize(
    'fixture', [EMPTY_FIXTURE, MINIMAL_FIXTURE], ids=['empty', 'minimal']
)
def test_every_template_renders_without_optional_entry_keys(fixture, now):
    # StrictUndefined raises on any optional key read without a guard.
    pages = _pages(fixture, now)

    assert set(pages) == set(RENDERED)
    assert 'class="brand"' in pages['layout/_header.html']
    assert 'id="hero-title"' in pages['site/homepage/_hero.html']
    assert 'class="err-title"' in pages['error/not_found.html']


def test_teaser_html_leaks_nothing(teaser):
    html = ''.join(teaser.values())

    assert _leaks(teaser) == []
    assert TEASER_LINE in teaser['site/homepage/_hero.html']
    assert TEASER_LINE in html


def test_leak_tokens_all_appear_once_revealed(revealed):
    # Control for the leak checks: each token is something the revealed
    # page really prints, so its absence from the teaser means something.
    assert _leaks(revealed) == list(LEAK_TOKENS)


@pytest.mark.parametrize(
    ('now', 'party_id'),
    [
        pytest.param(BEFORE_TEASER, 'gv-37', id='before-teaser'),
        pytest.param(AFTER_END, 'gv-37', id='after-party'),
        pytest.param(REVEALED, 'gv-38', id='other-party'),
    ],
)
def test_neutral_html_leaks_nothing(now, party_id):
    party = SimpleNamespace(
        id=party_id,
        title=PARTY.title,
        starts_at=PARTY.starts_at,
        ends_at=PARTY.ends_at,
    )

    pages = _pages(RENTE_FIXTURE, now, party=party)

    assert _leaks(pages) == []
    assert pages['layout/_topic_tokens.html'].strip() == ''


def test_revealed_fills_every_slot(revealed):
    header = revealed['layout/_header.html']
    footer = revealed['layout/_footer.html']
    overlays = revealed['layout/_look_overlays.html']
    tokens = revealed['layout/_topic_tokens.html']
    hero = revealed['site/homepage/_hero.html']

    assert [_text(e) for e in _select(header, css_class='tp-pun')] == ['RENT']
    assert _text(_select(header, css_class='wordmark')[0]) == 'genial verRENTet'
    assert [_text(e) for e in _select(hero, css_class='tp-pun')] == ['RENT']

    assert _select(footer, css_class='tp-foot')
    assert TOPIC_LINE in footer
    assert TOPIC_LINE in hero
    assert _select(hero, css_class='tp-line')

    ticks = _select(overlays, css_class='tp-tick')
    assert len(ticks) == 1
    assert 'Rente' in _text(ticks[0])

    for declaration in (
        '--tp:#a78bfa',
        '--tp-on-dark:#c4b5fd',
        '--tp-on-light:#6d28d9',
        '--tp-glow:rgba(167, 139, 250, .38)',
    ):
        assert declaration in tokens

    art = _select(hero, 'svg')
    assert [e for e in art if e.is_inside('crt') and e.is_inside('tp-art')]
    assert [
        e for e in art if e.is_inside('tp-frame') and e.is_inside('tp-art')
    ]
    assert hero.count(ART_MARKER) == 2


def test_revealed_page_without_optional_keys_uses_default_lines():
    pages = _pages(MINIMAL_FIXTURE, REVEALED)

    assert TOPIC_LINE in pages['layout/_footer.html']
    assert _select(pages['error/not_found.html'], css_class='tp-line') == []
    assert 'og:image' not in pages['layout/_topic_tokens.html']


def test_revealed_page_prints_the_optional_lines_when_set():
    pages = _pages(OPTIONALS_FIXTURE, REVEALED)

    foot = _select(pages['layout/_footer.html'], css_class='tp-foot')
    assert [_text(e) for e in foot] == ['Diesmal wird alles anders.']
    error = _select(pages['error/not_found.html'], css_class='tp-line')
    assert [_text(e) for e in error] == ['Hier gibt es nichts zu sehen.']
    tokens = pages['layout/_topic_tokens.html']
    assert (
        '<meta property="og:image" content="/site_file/style/og/9c1e.png">'
        in tokens
    )


def test_teaser_page_prints_only_the_teaser_text_of_the_optional_keys():
    pages = _pages(OPTIONALS_FIXTURE, TEASER)
    html = ''.join(pages.values())

    assert f'{TEASER_LINE} Haltet die Augen offen.' in html
    for text in (
        OPTIONALS_SET['footer_line'],
        OPTIONALS_SET['error_line'],
        OPTIONALS_SET['og_image'],
    ):
        assert text not in html
    assert _leaks(pages) == []


@pytest.mark.parametrize(
    'now',
    [
        pytest.param(BEFORE_TEASER, id='none'),
        pytest.param(TEASER, id='teaser'),
        pytest.param(REVEALED, id='on'),
    ],
)
def test_brand_name_kept_in_accessible_name(now):
    header = _pages(RENTE_FIXTURE, now)['layout/_header.html']

    brand = _select(header, 'a', 'brand')

    assert len(brand) == 1
    assert brand[0].attrs['aria-label'] == BRAND_LABEL


# ---------------------------------------------------------------- entries


@pytest.mark.parametrize('entry', ENTRIES)
def test_topic_entry_has_the_documented_shape(entry):
    keys = set(entry)

    assert REQUIRED_KEYS <= keys, f'missing: {sorted(REQUIRED_KEYS - keys)}'
    assert keys <= REQUIRED_KEYS | OPTIONAL_KEYS, (
        f'unknown (a typo is silently ignored): '
        f'{sorted(keys - REQUIRED_KEYS - OPTIONAL_KEYS)}'
    )
    assert datetime(*entry['teaser_at']) < datetime(*entry['announce_at'])


@pytest.mark.parametrize('entry', ENTRIES)
def test_topic_colour_steps_meet_aa(entry):
    dark = _contrast_message(entry, 'on_dark', _dark_requirements())
    light = _contrast_message(entry, 'on_light', _light_requirements())

    assert not dark, dark
    assert not light, light


@pytest.mark.parametrize('entry', ENTRIES)
def test_topic_hue_distinct_from_brand_and_danger(entry):
    colour = entry['colour']

    assert _hue_distance(colour, BRAND_ORANGE) >= 20, (
        f'{colour} is too close to the brand orange {BRAND_ORANGE}'
    )
    for look in ('owly-dark', 'owly-light'):
        danger = TOKEN_SETS[look]['--bad']
        assert _hue_distance(colour, danger) >= 15, (
            f'{colour} is too close to --bad {danger} of {look}'
        )


@pytest.mark.parametrize('entry', ENTRIES)
def test_topic_pun_fits_header(entry):
    assert len(entry['pun']) == 3
    assert len(''.join(entry['pun'])) <= PUN_BUDGET


@pytest.mark.parametrize('entry', ENTRIES)
def test_topic_art_not_under_static(entry):
    art = entry['art']
    basename = art.rpartition('/')[2]
    static_names = {path.name for path in STATIC_DIR.rglob('*')}

    assert art.startswith('topics/')
    assert basename not in static_names


@pytest.mark.parametrize('entry', ENTRIES)
def test_topic_art_name_does_not_spell_the_topic(entry):
    basename = entry['art'].rpartition('/')[2]

    assert entry['name'].lower() not in basename.lower()


# ---------------------------------------------------------------- helpers


def test_hue_distance_wraps_around_the_colour_wheel():
    assert _hue_distance('#ff0000', '#00ff00') == pytest.approx(120)
    assert _hue_distance('#ff0000', '#00ffff') == pytest.approx(180)
    # 356 degrees and 4 degrees are 8 apart, not 352.
    assert _hue_distance('#ff0010', '#ff1000') < 10


def test_nearest_lightness_is_the_closest_passing_one():
    # Mid grey passes against #808080 on the dark side (about 20 %) and on
    # the light side (about 89 %); the dark side is the nearer of the two.
    requirements = [('mid grey', '#808080', 3.0)]

    lightness, candidate = _nearest_passing_lightness('#7a7a7a', requirements)

    assert 10 < lightness < 30
    assert not _contrast_failures(candidate, requirements)
    toward_original = _with_lightness('#7a7a7a', (lightness + 0.1) / 100)
    assert _contrast_failures(toward_original, requirements)


def test_contrast_failure_proposes_a_lightness_that_passes():
    requirements = _dark_requirements()

    message = _contrast_message(
        {'party_id': 'x', 'on_dark': '#444444'}, 'on_dark', requirements
    )

    assert 'nearest passing HSL lightness is' in message
    proposed = _nearest_passing_lightness('#444444', requirements)
    assert proposed is not None
    assert not _contrast_failures(proposed[1], requirements)
    assert proposed[1] in message
    # A passing colour yields no message at all.
    assert not _contrast_message(RENTE, 'on_dark', requirements)
