"""
tests.unit.services.lan_tournament.test_seeding_copy_german
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from pathlib import Path

from babel.messages.pofile import read_po
import pytest


_PO_PATH = (
    Path(__file__).resolve().parents[4]
    / 'byceps'
    / 'translations'
    / 'de'
    / 'LC_MESSAGES'
    / 'messages.po'
)


@pytest.fixture(scope='module')
def catalog():
    with _PO_PATH.open('rb') as f:
        return read_po(f)


# fmt: off
@pytest.mark.parametrize(
    ('msgid', 'expected'),
    [
        (
            'The check character does not match. Check for a typo.',
            'Das Prüfzeichen passt nicht. Bitte auf Tippfehler prüfen.',
        ),
        (
            'This code belongs to a different roster.',
            ('Dieser Code gehört zu einer anderen Teilnehmerliste. '
             'Seitdem ist jemand beigetreten oder ausgetreten.'),
        ),
        (
            ('Rebuilt from the code: %(format)s, %(n)s participants, '
             '%(fixes)s.'),
            ('Aus dem Code nachgestellt: %(format)s, %(n)s Spieler, '
             '%(fixes)s.'),
        ),
        (
            'The seeding is locked once the tournament has started.',
            ('Die Erstplatzierung ist gesperrt; Änderungen laufen über die '
             'Ergebniskorrektur.'),
        ),
        (
            ('A match of this round already has a result. Its seeding is '
             'locked.'),
            ('Ein Ergebnis ist bestätigt. Die Platzierung ist gesperrt; '
             'Änderungen laufen über die Ergebniskorrektur.'),
        ),
        (
            ('Cannot start tournament without generated brackets. Generate '
             'brackets first.'),
            'Zuerst die Erstplatzierung in der Setzliste generieren.',
        ),
        (
            ('Double elimination playoffs need at least 4 qualifiers in '
             'total.'),
            'Doppeltes K.-o. braucht mindestens 4 Qualifizierte.',
        ),
        (
            ('This tournament is ongoing. Only description, image, and '
             'ruleset can be changed.'),
            ('Das Turnier läuft. Nur Beschreibung, Bild und Regelwerk lassen '
             'sich noch ändern.'),
        ),
        ('Higher score is better', 'Höhere Punktzahl ist besser.'),
        ('Lower score is better', 'Niedrigere Punktzahl ist besser.'),
        ('Move higher', 'Nach oben'),
        ('Move lower', 'Nach unten'),
        ('At', 'Zeit'),
    ],
)
# fmt: on
def test_k2_german_for_copy_items(catalog, msgid, expected):
    message = catalog.get(msgid)

    assert message is not None
    assert message.string == expected
