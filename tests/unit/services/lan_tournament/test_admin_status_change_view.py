"""
tests.unit.services.lan_tournament.test_admin_status_change_view
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The status-change flash must not be half translated.

``tournament_service.change_status`` reports failures as plain English
msgids -- the transition errors from ``tournament_domain_service`` and
the bracket gate in ``change_status`` alike. Those sentences are
hand-added catalogue entries, so the view is the only place the
translation can happen. Interpolating one straight into the translated
wrapper left an admin reading a German sentence with an English tail.

``check_translations.py`` cannot catch this: it scans ``gettext()``
call sites, and these msgids never appear at one.
"""

from unittest.mock import MagicMock, patch

import pytest
from flask import Flask

from byceps.services.lan_tournament.models.tournament import TournamentID
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.util.result import Err

from tests.helpers import generate_uuid


TOURNAMENT_ID = TournamentID(generate_uuid())

_V = 'byceps.services.lan_tournament.blueprints.admin.views'

# The sentence change_status returns when the bracket was never
# generated. It is a real catalogue msgid -- see
# byceps/translations/de/LC_MESSAGES/messages.po.
SERVICE_ERROR = (
    'Cannot start tournament without generated brackets. '
    'Generate brackets first.'
)

CATALOGUE = {
    'Status change failed: %(error)s': 'Statuswechsel fehlgeschlagen: %(error)s',
    SERVICE_ERROR: (
        'Turnier kann nicht gestartet werden ohne generierten '
        'Turnierbaum. Generiere zuerst den Turnierbaum.'
    ),
}


@pytest.fixture(scope='module')
def app():
    a = Flask(__name__)
    a.config['TESTING'] = True
    a.config['LOCALE'] = 'en'
    return a


def _translate(msg, **kw):
    translated = CATALOGUE.get(msg, msg)
    return translated % kw if kw else translated


def _call_change_status(app, service_error: str):
    from byceps.services.lan_tournament.blueprints.admin import views

    tournament = MagicMock()
    tournament.id = TOURNAMENT_ID

    with app.test_request_context('/'):
        with (
            patch(f'{_V}.g'),
            patch(f'{_V}.gettext', side_effect=_translate),
            patch(f'{_V}.flash_error') as mock_flash_error,
            patch(f'{_V}.redirect_to'),
            patch(f'{_V}.tournament_service') as mock_service,
            patch(f'{_V}._get_tournament_or_404') as mock_get_tournament,
        ):
            mock_get_tournament.return_value = tournament
            mock_service.change_status.return_value = Err(service_error)

            views._change_status(
                str(TOURNAMENT_ID), TournamentStatus.ONGOING
            )

    return mock_flash_error


def test_status_change_error_is_translated_whole(app):
    """Both halves of the flash, not just the wrapper."""
    mock_flash_error = _call_change_status(app, SERVICE_ERROR)

    (flashed,) = mock_flash_error.call_args.args
    assert flashed.startswith('Statuswechsel fehlgeschlagen')
    assert 'Turnier kann nicht gestartet werden' in flashed
    assert 'Generate brackets first' not in flashed


def test_ungenerated_bracket_reports_the_catalogued_sentence(app):
    """The start gate must keep returning a translatable msgid.

    ``validate_bracket_for_start`` replaced a plain
    ``_has_bracket_generated()`` check, and composing its violations
    into 'Cannot start tournament: <diagnostics>' matched no catalogue
    entry -- regressing a previously translated flash to English for
    the one failure admins actually hit.
    """
    from byceps.services.lan_tournament import tournament_service

    tournament = MagicMock()
    tournament.id = TOURNAMENT_ID
    tournament.tournament_status = TournamentStatus.REGISTRATION_CLOSED
    tournament.game_format.requires_bracket_generation = True

    _S = 'byceps.services.lan_tournament.tournament_service'
    with (
        patch(f'{_S}.tournament_repository') as mock_repo,
        patch(f'{_S}.tournament_domain_service') as mock_domain,
        patch(f'{_S}.tournament_match_service') as mock_match_svc,
    ):
        mock_repo.get_tournament.return_value = tournament
        mock_domain.change_tournament_status.return_value = MagicMock(
            is_err=lambda: False, unwrap=lambda: (MagicMock(),)
        )
        mock_match_svc.validate_bracket_for_start.return_value = [
            'no matches generated'
        ]

        result = tournament_service.change_status(
            TOURNAMENT_ID, TournamentStatus.ONGOING
        )

    assert result.is_err()
    assert result.unwrap_err() == SERVICE_ERROR


def test_invalid_transition_reports_a_static_catalogued_msgid():
    """The refused-transition error must be a real catalogue msgid.

    The view flashes this through ``gettext(error_message)``, and
    these service sentences are hand-added catalogue entries -- babel
    never sees them, because they appear at no ``gettext()`` call
    site. An f-string naming the two statuses renders one of ~30
    sentences, none of which is a msgid, so the flash came back out
    as a German wrapper with an English tail: exactly the defect the
    inner ``gettext()`` was added to fix.
    """
    import pathlib

    from babel.messages.pofile import read_po

    from byceps.services.lan_tournament import tournament_domain_service

    result = tournament_domain_service.validate_status_transition(
        TournamentStatus.COMPLETED, TournamentStatus.REGISTRATION_OPEN
    )
    assert result.is_err()
    message = result.unwrap_err()

    # Static: the same sentence whichever pair was refused.
    other = tournament_domain_service.validate_status_transition(
        TournamentStatus.ONGOING, TournamentStatus.REGISTRATION_OPEN
    )
    assert other.is_err()
    assert other.unwrap_err() == message

    po_path = pathlib.Path(
        'byceps/translations/de/LC_MESSAGES/messages.po'
    )
    with po_path.open('rb') as f:
        catalogue = read_po(f)
    entry = catalogue.get(message)
    assert entry is not None, (
        f'{message!r} is not a msgid in {po_path}'
    )
    assert entry.string, f'{message!r} has no German translation'
