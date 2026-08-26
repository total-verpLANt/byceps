import functools
import pathlib
import re

from babel.messages.pofile import read_po
import pytest


_PO_PATH = (
    pathlib.Path(__file__).resolve().parents[4]
    / 'byceps/translations/de/LC_MESSAGES/messages.po'
)

# The design prints these symbols beside a word. They are decoration, never
# part of a msgstr.
_SYMBOLS = re.compile(r'[○◐✓⧗■◉☐☑←]\s*')
_SEPARATORS = re.compile(r'\s[—·/]\s')

# German copy of the design sheet, per surface (`DESIGN_SPEC_F04.md`
# §2.1-2.5), as the design prints it. A composite string counts as met when
# each of its parts is a msgstr.
# fmt: off
_COPY_SHEET = {
    'card': (
        'Bereitschaft',
        '○ Nicht bereit',
        '◐ Teilweise bereit — Seite A bereit',
        '◐ Teilweise bereit — Seite B bereit',
        '✓ Beide bereit',
        'Wartet auf Gegner',
        (
            'Die Zuweisung sagt nichts über die Anwesenheit vor Ort aus und'
            ' auch nicht, ob das Spiel begonnen hat.'
        ),
        'Ursprünglich besetzt seit',
        'Aktuelle Paarung seit',
        'Seite A',
        'Seite B',
        '✓ Bereit',
        '○ Nicht bereit',
        'Bereit seit',
        'Ich bin bereit · Ich bin nicht bereit',
        'Bereit (für diese Seite) · Nicht bereit (für diese Seite)',
        'Das Turnier ist pausiert; die Bereitschaft ist schreibgeschützt.',
        (
            'Diese Partie ist abgeschlossen; die Bereitschaft ist'
            ' schreibgeschützt.'
        ),
        'Bereitschaft gemeldet. · Bereitschaft widerrufen.',
    ),
    'filter bar': (
        'Partien filtern',
        'Partien',
        (
            'Wartet auf Gegner · Nicht bereit · Teilweise bereit · Beide bereit'
            ' · Offen (ohne Bereitschaft) · Beendet · Alle'
        ),
        '☐ Meine Partien',
        '☑ Meine Partien',
        'Es wurden noch keine Partien erstellt.',
        'Keine Partien mit diesem Bereitschaftsstatus.',
    ),
    'status column': (
        'Partie',
        'Teilnehmer',
        'Ergebnis',
        'Status',
        'Bestätigt',
        'DEFWIN',
        'Abgeschlossen',
        'Abgebrochen',
        '○ Nicht bereit',
        '◐ Teilweise bereit',
        'Seite A bereit · Seite B nicht bereit',
        'Seite B bereit · Seite A nicht bereit',
        '✓ Beide bereit',
        'Wartet auf Gegner',
        'Offen (ohne Bereitschaft)',
    ),
    'bracket': (
        'Nicht bereit',
        'Seite A bereit',
        'Seite B bereit',
        'Beide bereit',
        'Wartet auf Gegner',
        'Abgeschlossen',
        'DEFWIN',
        'Bereitschaft der Partien',
        'Partie',
        'Runde',
        'Spiel um Platz 3',
    ),
    'admin': (
        '← Turnier',
        '← Partien',
        'Partien',
        'Details',
        '⧗ Wartet auf Gegner',
        '○ Nicht bereit',
        '◐ Teilweise bereit',
        '✓ Beide bereit',
        '⧗ Offen (ohne Bereitschaft)',
        '■ Beendet',
        '◉ Alle',
        'Bestätigt',
        'Keine Partien mit diesem Bereitschaftsstatus.',
        'Bereitschaft der Partien',
        'Seite A bereit seit',
        'Seite B bereit seit',
    ),
}
# fmt: on


@functools.cache
def _msgstrs():
    with _PO_PATH.open('rb') as f:
        catalog = read_po(f, locale='de')
    found = set()
    for message in catalog:
        if message.id and not message.fuzzy:
            forms = message.string
            found.update(forms if isinstance(forms, tuple) else (forms,))
    return found


def _parts(entry):
    return [part for part in _SEPARATORS.split(_SYMBOLS.sub('', entry)) if part]


@pytest.mark.parametrize('section', _COPY_SHEET)
def test_design_copy_sheet_strings_have_german(section):
    missing = [
        part
        for entry in _COPY_SHEET[section]
        for part in _parts(entry)
        if part not in _msgstrs()
    ]

    assert missing == []
