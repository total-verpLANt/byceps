import functools
import pathlib
import re
from types import SimpleNamespace

from babel.messages.extract import extract_from_file
from babel.messages.pofile import read_po
from jinja2 import Environment, FileSystemLoader, StrictUndefined
import pytest

from byceps.services.lan_tournament.models.bracket import Bracket


_ROOT = pathlib.Path(__file__).resolve().parents[4]
_TEMPLATES = _ROOT / 'byceps/services/core/blueprints/common/templates'
_MACRO = _TEMPLATES / 'macros/lan_tournament.html'
_PO_PATH = _ROOT / 'byceps/translations/de/LC_MESSAGES/messages.po'

_DEFWIN_TOOLTIP = 'Free win: advances without an opponent.'

# Untranslated msgids the macro may still use. Keep it empty: every msgid
# of the macro needs German in the catalogue.
KNOWN_UNTRANSLATED: frozenset[str] = frozenset()

_BADGE_SPAN = re.compile(r"<span class='badge-defwin'.*?</span>", re.DOTALL)
_RENDERED_BADGE = re.compile(
    r"<span class='badge-defwin'\s+title='([^']*)'>\s*([^<]*?)\s*</span>"
)


@functools.cache
def _catalog():
    with _PO_PATH.open('rb') as f:
        return read_po(f, locale='de')


def _german(msgid):
    """Return the msgstr, or `None` if the catalogue has no German for it."""
    message = _catalog().get(msgid)
    if message is None or message.fuzzy:
        return None
    forms = message.string
    if not isinstance(forms, tuple):
        forms = (forms,)
    if not all(form and form.strip() for form in forms):
        return None
    return forms[0]


@functools.cache
def _macro_msgids():
    msgids = set()
    for _, message, _, _ in extract_from_file(
        'jinja2',
        _MACRO,
        options={'extensions': 'jinja2.ext.i18n', 'newstyle_gettext': 'true'},
    ):
        singular = message[0] if isinstance(message, tuple) else message
        if singular:
            msgids.add(singular)
    return frozenset(msgids)


def _translate(msgid):
    return _german(msgid) or msgid


def _bye_match_data():
    match = SimpleNamespace(
        id='match-1',
        round=0,
        match_order=0,
        bracket=Bracket.WINNERS,
        confirmed_by=None,
    )
    contestant = SimpleNamespace(
        team_id=None, participant_id='participant-1', score=None
    )
    return [{'match': match, 'contestants': [contestant]}]


def _render(call):
    env = Environment(
        loader=FileSystemLoader(_TEMPLATES),
        undefined=StrictUndefined,
        autoescape=True,
    )
    env.globals['_'] = _translate
    env.filters['dateformat'] = lambda value: value
    env.filters['timeformat'] = lambda value, *args: value
    source = (
        '{% from "macros/lan_tournament.html" import '
        'render_bracket, render_simple_bracket_list, render_de_bracket, '
        'render_simple_de_bracket_list %}{{ ' + call + ' }}'
    )
    return env.from_string(source).render(
        match_data=_bye_match_data(), tournament=None
    )


def test_bracket_macro_msgids_have_german_or_are_known_gaps():
    msgids = _macro_msgids()

    assert {'DEFWIN', _DEFWIN_TOOLTIP} <= msgids
    missing = {msgid for msgid in msgids if _german(msgid) is None}
    assert sorted(missing - KNOWN_UNTRANSLATED) == []


def test_known_untranslated_list_only_shrinks():
    msgids = _macro_msgids()

    assert sorted(KNOWN_UNTRANSLATED - msgids) == []
    translated = {msgid for msgid in KNOWN_UNTRANSLATED if _german(msgid)}
    assert sorted(translated) == []


def test_defwin_badges_use_design_label_and_german_tooltip():
    source = _MACRO.read_text(encoding='utf-8')

    spans = _BADGE_SPAN.findall(source)

    assert spans
    for span in spans:
        assert "{{ _('DEFWIN') }}" in span
        assert f'{{{{ _("{_DEFWIN_TOOLTIP}") }}}}' in span
    assert 'Free Win - Opponent advanced automatically' not in source
    assert 'Free Win' not in source
    assert _german(_DEFWIN_TOOLTIP) == 'Freilos: rückt ohne Gegner weiter.'


# fmt: off
@pytest.mark.parametrize('call', [
    'render_bracket(match_data, tournament)',
    'render_simple_bracket_list(match_data)',
    'render_de_bracket(match_data, tournament)',
    'render_simple_de_bracket_list(match_data)',
])
# fmt: on
def test_rendered_bye_badge_reads_defwin_with_german_tooltip(call):
    html = _render(call)

    assert _RENDERED_BADGE.findall(html) == [
        ('Freilos: rückt ohne Gegner weiter.', 'DEFWIN')
    ]
