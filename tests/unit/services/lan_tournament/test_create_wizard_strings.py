from pathlib import Path
import re

from babel.messages.pofile import read_po
from flask_babel import force_locale
import pytest

from byceps.services.lan_tournament.lan_tournament_view_helpers import (
    build_create_wizard_strings,
)


_ROOT = Path(__file__).resolve().parents[4]
_BEHAVIOR = _ROOT / 'byceps/static/behavior'
_WIZARD_JS = _BEHAVIOR / 'lan_tournament_create_wizard.js'
_IMAGE_JS = _BEHAVIOR / 'lan_tournament_create_wizard_image.js'
_RULES_JS = _BEHAVIOR / 'lan_tournament_create_wizard_rules.js'
_PO = _ROOT / 'byceps/translations/de/LC_MESSAGES/messages.po'

_LITERAL = r"""(?:'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*")"""
# One or more string literals joined by `+`, as the JS splits long msgids.
_CONCAT = rf'{_LITERAL}(?:\s*\+\s*{_LITERAL})*'
_PLACEHOLDER = re.compile(r'%\((\w+)\)[sd]')


@pytest.fixture
def ctx(app):
    with app.app_context(), app.test_request_context('/'):
        yield


def _unquote(literal: str) -> str:
    body = literal[1:-1]
    return re.sub(r'\\(.)', r'\1', body)


def _join_literals(concat: str) -> str:
    return ''.join(_unquote(m) for m in re.findall(_LITERAL, concat))


def _find_msgids(source: str, prefix: str) -> set[str]:
    pattern = re.compile(prefix + rf'\s*({_CONCAT})')
    return {_join_literals(m.group(1)) for m in pattern.finditer(source)}


def _js_t_literals(path: Path) -> set[str]:
    return _find_msgids(path.read_text(encoding='utf-8'), r'\bt\(')


def _rules_msgids() -> set[str]:
    """Return every msgid literal the rules module can put in a message."""
    source = _RULES_JS.read_text(encoding='utf-8')
    found = set()
    found |= _find_msgids(source, r'\bmessage\(')
    found |= _find_msgids(source, r'\bmsgid:')
    found |= _find_msgids(source, r'\bvar MSG_\w+\s*=')
    # `add(field, msgid, ...)`: the field is a literal or an expression.
    found |= _find_msgids(source, r'\badd\(\s*[^,()]+(?:\[\d\])?,')
    # The label maps are themselves msgids: the caller translates params.
    for block in re.findall(
        r'var \w+_LABELS = \{(.*?)\};', source, flags=re.DOTALL
    ):
        found |= {
            _join_literals(m) for m in re.findall(rf'\w+:\s*({_CONCAT})', block)
        }
    return found


def _read_catalog():
    with _PO.open('rb') as f:
        return read_po(f)


def test_every_js_t_literal_is_served(ctx):
    strings = build_create_wizard_strings()
    served = set(strings)

    for path in (_WIZARD_JS, _IMAGE_JS):
        literals = _js_t_literals(path)

        assert literals, path.name
        assert literals <= served, (path.name, sorted(literals - served))


def test_dynamic_js_msgids_are_served(ctx):
    # Invisible to the static `t('...')` scan: `t(pair[1])`, `t(STEP_TITLES[i])`,
    # and `t(item.label)` for rules-module field labels.
    served = set(build_create_wizard_strings())

    assert {'This party', 'All parties'} <= served
    assert {
        'Basics',
        'Competition',
        'Participants',
        'Scoring and groups',
        'Review and create',
    } <= served


def test_attention_count_serves_singular_and_plural(ctx):
    served = build_create_wizard_strings()

    assert served['%(n)s entry needs attention'] == (
        '%(n)s entry needs attention'
    )
    assert served['%(n)s entries need attention'] == (
        '%(n)s entries need attention'
    )


def test_attention_count_has_german_plural_forms():
    catalog = _read_catalog()

    message = catalog.get('%(n)s entry needs attention')

    assert message is not None
    assert message.pluralizable
    assert message.id == (
        '%(n)s entry needs attention',
        '%(n)s entries need attention',
    )
    assert message.string == (
        '%(n)s Angabe braucht noch Aufmerksamkeit',
        '%(n)s Angaben brauchen noch Aufmerksamkeit',
    )


def test_every_rules_module_msgid_is_served(ctx):
    served = set(build_create_wizard_strings())
    msgids = _rules_msgids()

    assert len(msgids) >= 30
    assert msgids <= served, sorted(msgids - served)


def test_rules_msgid_scan_finds_known_shapes():
    msgids = _rules_msgids()

    assert 'Please enter a name.' in msgids  # add(field, literal)
    assert 'Required for Free-for-All.' in msgids
    assert (  # concatenated literal
        'Add points for at least place 1.' in msgids
    )
    assert 'Places %(from)s–%(to)s get 0 points.' in msgids  # message()
    assert (
        'Score ordering no longer applies. It is only saved for Highscore.'
        in msgids
    )
    assert 'Must be at least "%(other)s" (%(n)s).' in msgids  # MSG_ constant
    assert 'Everyone plays everyone' in msgids  # label map
    assert 'Min. players per team' in msgids  # label map


def test_served_string_keys_equal_msgids_in_english(ctx):
    with force_locale('en'):
        strings = build_create_wizard_strings()

    assert strings
    assert {key: value for key, value in strings.items() if key != value} == {}


def _german_forms(catalog, msgid):
    """Return the German strings for a served msgid, plural forms included."""
    message = catalog.get(msgid)
    if message is None:
        for candidate in catalog:
            if isinstance(candidate.id, tuple) and candidate.id[1] == msgid:
                message = candidate
                break
    if message is None:
        return None
    strings = message.string
    return [strings] if isinstance(strings, str) else list(strings)


def test_served_strings_have_german_translation(ctx):
    catalog = _read_catalog()

    missing = [
        msgid
        for msgid in build_create_wizard_strings()
        if not (forms := _german_forms(catalog, msgid)) or not all(forms)
    ]

    assert missing == []


def test_served_strings_keep_placeholders_in_german(ctx):
    catalog = _read_catalog()

    mismatched = [
        msgid
        for msgid in build_create_wizard_strings()
        if (forms := _german_forms(catalog, msgid)) is not None
        and any(
            set(_PLACEHOLDER.findall(msgid)) != set(_PLACEHOLDER.findall(form))
            for form in forms
        )
    ]

    assert mismatched == []


# --- Wording of the refusal and the created flash ---

_REFUSAL_MSGIDS = (
    (
        'Request #%(number)s is no longer accepted. No tournament was '
        'created; your entries are kept.'
    ),
    (
        'Request #%(id)s is no longer accepted. No tournament was created; '
        'your entries are kept.'
    ),
    'Request #%(id)s is no longer accepted.',
)
_CREATED_FLASH_MSGID = 'Tournament "%(name)s" has been created as a draft.'


def _german(msgid: str) -> str:
    message = _read_catalog().get(msgid)
    assert message is not None and message.string, msgid
    return message.string


@pytest.mark.parametrize('msgid', _REFUSAL_MSGIDS)
def test_refusal_says_anfrage_not_antrag(msgid):
    text = _german(msgid)

    assert text.startswith('Anfrage #')
    assert 'Antrag' not in text
    assert 'ist nicht mehr angenommen' in text


def test_refusal_pieces_read_as_the_drafted_sentences():
    assert _german('%(name)s withdrew it at %(time)s.') == (
        '%(name)s hat sie um %(time)s zurückgezogen.'
    )
    assert _german('No tournament was created; your entries are kept.') == (
        'Es wurde kein Turnier erstellt, deine Eingaben sind erhalten.'
    )
    assert _german('Create without link to the request') == (
        'Ohne Verknüpfung zur Anfrage erstellen'
    )
    assert _german('View request') == 'Anfrage ansehen'


def test_created_flash_is_the_drafted_sentence_with_closing_quote():
    text = _german(_CREATED_FLASH_MSGID)

    assert text == 'Das Turnier „%(name)s“ wurde als Entwurf erstellt.'
    assert '“' in text
    assert '”' not in text


def test_create_view_flashes_the_drafted_msgid():
    source = (
        _ROOT / 'byceps/services/lan_tournament/blueprints/admin/views.py'
    ).read_text(encoding='utf-8')

    assert '\'Tournament "%(name)s" has been created as a draft.\'' in source
    assert '\'Tournament "%(name)s" has been created.\'' not in source
