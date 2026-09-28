import functools
import pathlib

from babel.messages.extract import extract_from_file
from babel.messages.pofile import read_po
import pytest


_ROOT = pathlib.Path(__file__).resolve().parents[4]
_PO_PATH = _ROOT / 'byceps/translations/de/LC_MESSAGES/messages.po'

_BOTE = _ROOT / 'sites/totalverplant-36/template_overrides/site/lan_tournament'
_SITE = _ROOT / 'byceps/services/lan_tournament/blueprints/site'
_ADMIN = _ROOT / 'byceps/services/lan_tournament/blueprints/admin'
_ADMIN_REQUESTS = _ADMIN / 'templates/admin/lan_tournament'
_SERVICES = _ROOT / 'byceps/services/lan_tournament'

_SURFACE_FILES = {
    'site': (
        _BOTE / 'propose_form.html',
        _BOTE / 'my_requests.html',
        _SITE / 'forms.py',
        _SITE / 'views.py',
    ),
    'admin': (
        _ADMIN_REQUESTS / 'requests_for_party.html',
        _ADMIN_REQUESTS / 'view_request.html',
        _ADMIN_REQUESTS / 'update_request_form.html',
        _ADMIN / 'templates/layout/admin/lan_tournament.html',
        _ADMIN / 'forms.py',
        _ADMIN / 'views.py',
    ),
    'service': (_SERVICES / 'tournament_request_domain_service.py',),
    'create': (_ADMIN_REQUESTS / 'create_form.html',),
}

_EXTRACTION_KEYWORDS = {
    '_': None,
    'gettext': None,
    'lazy_gettext': None,
    'ngettext': (1, 2),
}


# Surface, msgid, German: one row per msgid of the drafts' copy.
# fmt: off
_COPY_ROWS = [
    ('site',       'What it is about', 'Worum geht’s'),
    ('site',       'Tournament name', 'Name des Turniers'),
    ('site/admin', 'Tournament mode', 'Turniermodus'),
    ('site/admin', 'Preferred start', 'Gewünschter Start'),
    ('site/admin', 'Preferred end', 'Gewünschtes Ende'),
    ('site/admin', 'Special rules', 'Sonderregeln'),
    ('site/admin', 'Notes for the orga', 'Notizen an die Orga'),
    ('site',       'e.g. “like last year”', 'z. B. „wie letztes Jahr“'),
    ('site/admin', 'Single knockout', 'Einfaches K.-o.'),
    ('site/admin', 'Double knockout', 'Doppeltes K.-o.'),
    ('site/admin', 'Everyone plays everyone', 'Jeder gegen jeden'),
    ('site/admin', 'No knockout', 'Ohne K.-o.'),
    ('site/admin', 'only %(format)s', 'nur %(format)s'),
    ('site/admin', 'not with %(format)s', 'nicht bei %(format)s'),
    ('site',       'Struck-through modes do not fit “%(format)s”.', 'Durchgestrichene Modi passen nicht zu „%(format)s“.'),
    ('site/admin', '2 to {max} · {capacity} seats at the party', '2 bis {max} · {capacity} Plätze auf der Party'),
    ('site/admin', '2 to {max} teams · {capacity} seats ÷ {size}', '2 bis {max} Teams · {capacity} Plätze ÷ {size}'),
    ('site/admin', 'Team size too large for {capacity} seats', 'Teamgröße zu groß für {capacity} Plätze'),
    ('site',       'Required field', 'Pflichtfeld'),
    ('site',       'Enter at least %(min)s.', 'Mindestens %(min)s'),
    ('site',       'End is before the start (%(time)s)', 'Ende liegt vor dem Start (%(time)s)'),
    ('site',       'is missing', 'fehlt'),
    ('site',       'is too small', 'ist zu klein'),
    ('site',       'is before the start', 'liegt vor dem Start'),
    ('site',       'Edited by you', 'Von dir bearbeitet'),
    ('site',       'Edited by the orga', 'Von der Orga bearbeitet'),
    ('site',       'The orga accepted your request on %(date)s and is setting up the tournament. Changes are no longer possible. Contact the orga if something important changed.', 'Die Orga hat deine Anfrage am %(date)s angenommen und legt das Turnier gerade an. Ändern geht nicht mehr. Schreib der Orga, wenn sich etwas Wichtiges geändert hat.'),
    ('site',       '%(size)s (Solo)', '%(size)s (Solo)'),
    ('site',       'changed', 'geändert'),
    ('site',       'Before: %(value)s ·', 'Vorher: %(value)s ·'),
    ('site',       'Party %(title)s', 'Party %(title)s'),
    ('site',       'Wish start', 'Wunschstart'),
    ('site',       'Accepted on %(date)s. The orga is setting up the tournament.', 'Angenommen am %(date)s. Die Orga legt das Turnier an.'),
    ('site',       'Full reason', 'Ganze Begründung'),
    ('site',       'No requests yet.', 'Noch keine Anfragen.'),
    ('site',       'Got an idea for the program? Propose a tournament, the orga will get back to you.', 'Du hast eine Idee fürs Programm? Schlag ein Turnier vor, die Orga meldet sich.'),
    ('admin',      'Open', 'Offen'),
    ('admin',      '%(num)s part.', '%(num)s Teiln.'),
    ('admin',      'Accepted · tournament not created yet', 'Angenommen · Turnier noch nicht erstellt'),
    ('admin',      'Note to the orga (not public)', 'Notiz an die Orga (nicht öffentlich)'),
    ('admin',      'Preview: tournament from this request', 'Vorschau: Turnier aus dieser Anfrage'),
    ('admin',      '%(num)s fields in the create form', '%(num)s Felder im Erstellformular'),
    ('admin',      '%(num)s from the request', '%(num)s aus der Anfrage'),
    ('admin',      '%(num)s still open', '%(num)s noch offen'),
    ('admin',      '%(num)s block creation', '%(num)s blockieren die Erstellung'),
    ('admin',      'Free-for-All needs points by placement and a max. group size.', 'Free-for-All braucht Punkte nach Platzierung und eine max. Gruppengröße.'),
    ('admin',      'The request supplies neither. Without these values the create form refuses.', 'Die Anfrage liefert beides nicht. Ohne diese Werte lehnt das Erstellformular ab.'),
    ('admin',      'Points sorting', 'Punkte-Sortierung'),
    ('admin',      'Advancers per group', 'Aufsteiger je Gruppe'),
    ('admin',      'Points in losers round', 'Punkte in Verliererrunde'),
    ('admin',      'Still required in the form: points by placement, max. group size', 'Im Formular noch Pflicht: Punkte nach Platzierung, max. Gruppengröße'),
    ('admin',      'Available once the tournament is created.', 'Verfügbar, sobald das Turnier erstellt ist.'),
    ('admin',      'Reject after all …', 'Doch ablehnen …'),
    ('admin',      '(required, visible to %(name)s)', '(Pflicht, für %(name)s sichtbar)'),
    ('admin',      'Changes are visible to %(name)s right away in the request. They are recorded in the history as an admin change.', 'Änderungen sieht %(name)s sofort in der Anfrage. Sie werden im Verlauf als Admin-Änderung vermerkt.'),
    ('admin',      'Only modes that fit the game format can be selected.', 'Nur zum Spielformat passende Modi sind wählbar.'),
    ('admin',      '1 = solo', '1 = Solo'),
    ('admin',      'Never publicly visible.', 'Nie öffentlich sichtbar.'),
    ('service',    'Preferred end time must not precede start time.', 'Gewünschtes Ende darf nicht vor dem Start liegen.'),
    ('service',    'Special rules must not exceed 2000 characters.', 'Sonderregeln dürfen 2000 Zeichen nicht überschreiten.'),
    ('service',    'Special rules must not contain control characters.', 'Sonderregeln dürfen keine Steuerzeichen enthalten.'),
    ('service',    'Notes must not exceed 2000 characters.', 'Notizen an die Orga dürfen 2000 Zeichen nicht überschreiten.'),
    ('service',    'Notes must not contain control characters.', 'Notizen an die Orga dürfen keine Steuerzeichen enthalten.'),
    ('create',     'Preferred end (not a field here):', 'Gewünschtes Ende (hier kein Feld):'),
    ('site/admin', 'Submitted', 'Eingereicht'),
    ('site',       'History', 'Verlauf'),
    ('site/admin', 'Limit', 'Limit'),
    ('site',       'End', 'Ende'),
    ('site/admin', 'Tournament start', 'Start'),
    ('site',       'Required', 'Pflicht'),
    ('site/admin', 'submitted %(date)s', 'eingereicht %(date)s'),
    ('admin',      'Tournament requests', 'Turnieranfragen'),
    ('admin',      'All', 'Alle'),
    ('admin',      'Status:', 'Status:'),
    ('admin',      'until %(date)s, %(time)s', 'bis %(date)s, %(time)s'),
    ('admin',      'Max. players', 'Max. Spieler'),
    ('admin',      'Reason', 'Begründung'),
    ('admin',      'not specified', 'nicht angegeben'),
]

# Surface, singular msgid, plural msgid, (German singular, German plural).
_PLURAL_ROWS = [
    ('admin', '%(num)s part.', '%(num)s part.',
     ('%(num)s Teiln.', '%(num)s Teiln.')),
    ('admin',
     '%(num)s required field is missing for the tournament',
     '%(num)s required fields are missing for the tournament',
     ('%(num)s Pflichtfeld fehlt fürs Turnier',
      '%(num)s Pflichtfelder fehlen fürs Turnier')),
]
# fmt: on


@functools.cache
def _catalog():
    with _PO_PATH.open('rb') as f:
        return read_po(f, locale='de')


def _forms(message):
    if isinstance(message.string, tuple):
        return message.string
    return (message.string,)


def _occurs_on(surface, needle):
    quoted = (f"'{needle}'", f'"{needle}"')
    return any(
        any(q in path.read_text(encoding='utf-8') for q in quoted)
        for path in _SURFACE_FILES[surface]
    )


def _label(row):
    surface, msgid = row[0], row[1]
    return f'{surface}: {msgid}'


def _german_or_none(message):
    if message is None or message.fuzzy:
        return None
    forms = _forms(message)
    return forms if all(forms) else None


@pytest.mark.parametrize(
    ('surface', 'msgid', 'german'),
    _COPY_ROWS,
    ids=[_label(row) for row in _COPY_ROWS],
)
def test_draft_copy_msgstr_matches(surface, msgid, german):
    message = _catalog().get(msgid)

    assert message is not None, f'no catalogue entry for {msgid!r}'
    assert not message.fuzzy, f'{msgid!r} is fuzzy, so it is not compiled'
    assert _forms(message) == (german,) * len(_forms(message))


@pytest.mark.parametrize(
    ('surface', 'msgid', 'msgid_plural', 'german'),
    _PLURAL_ROWS,
    ids=[_label(row) for row in _PLURAL_ROWS],
)
def test_draft_copy_plural_msgstr_matches(surface, msgid, msgid_plural, german):
    message = _catalog().get(msgid)

    assert message is not None, f'no catalogue entry for {msgid!r}'
    assert not message.fuzzy, f'{msgid!r} is fuzzy, so it is not compiled'
    assert message.id == (msgid, msgid_plural)
    assert message.string == german


def _usage_cases():
    needles = [(row[0], row[1]) for row in _COPY_ROWS + _PLURAL_ROWS]
    needles += [(row[0], row[2]) for row in _PLURAL_ROWS]
    return [
        (part, needle)
        for surface, needle in dict.fromkeys(needles)
        for part in surface.split('/')
    ]


@pytest.mark.parametrize(
    ('surface', 'msgid'), _usage_cases(), ids=lambda value: value
)
def test_draft_copy_msgid_used_on_its_surface(surface, msgid):
    assert _occurs_on(surface, msgid), (
        f'{msgid!r} is not used on the {surface} request surface'
    )


def test_request_surface_msgids_have_german():
    missing = set()
    for paths in _SURFACE_FILES.values():
        for path in paths:
            method = (
                'python' if path.suffix == '.py' else 'jinja2.ext:babel_extract'
            )
            options = (
                {}
                if path.suffix == '.py'
                else {'extensions': 'jinja2.ext.i18n'}
            )
            for _, message, _, _ in extract_from_file(
                method, path, keywords=_EXTRACTION_KEYWORDS, options=options
            ):
                msgid = message[0] if isinstance(message, tuple) else message
                if msgid and _german_or_none(_catalog().get(msgid)) is None:
                    missing.add(msgid)

    assert sorted(missing) == []
