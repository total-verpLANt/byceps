"""
tests.unit.services.lan_tournament.test_tournament_request_notifications
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Unit tests for tournament-request decision emails (accept/reject),
their idempotent snippet setup, and the setup-button visibility rule.
"""

from datetime import datetime, UTC
import pathlib
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from babel import Locale
from jinja2 import DictLoader, Environment, StrictUndefined
import pytest
from sqlalchemy.exc import IntegrityError

from byceps.services.brand.models import Brand, BrandID
from byceps.services.email.models import EmailConfig, NameAndAddress
from byceps.services.lan_tournament.events import (
    TournamentRequestAcceptedEvent,
    TournamentRequestRejectedEvent,
)
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.tournament_request import (
    TournamentRequest,
    TournamentRequestID,
    TournamentRequestStatus,
)
from byceps.services.lan_tournament.notification_handlers import (
    _on_tournament_request_accepted,
    _on_tournament_request_rejected,
)
from byceps.services.lan_tournament.signals import (
    tournament_request_accepted,
    tournament_request_rejected,
)
from byceps.services.lan_tournament.tournament_notification_service import (
    _REQUIRED_SNIPPET_PAIRS,
    create_match_ready_email_snippets,
    create_tournament_request_email_snippets,
    email_templates_exist,
    send_request_accepted_email,
    send_request_rejected_email,
    SNIPPET_NAME_BODY,
    SNIPPET_NAME_REQUEST_ACCEPTED_BODY,
    SNIPPET_NAME_REQUEST_ACCEPTED_SUBJECT,
    SNIPPET_NAME_REQUEST_REJECTED_BODY,
    SNIPPET_NAME_REQUEST_REJECTED_SUBJECT,
    SNIPPET_NAME_SUBJECT,
)
from byceps.services.party.models import PartyID
from byceps.services.snippet.models import SnippetScope
from byceps.services.user.models import UserID
from byceps.util.result import Err, Ok

from tests.helpers import generate_uuid


NOW = datetime(2025, 6, 15, 14, 0, 0)
NOW_UTC = datetime(2025, 6, 15, 14, 0, 0, tzinfo=UTC)

PARTY_ID = PartyID('test-party-2025')
PROPOSER_ID = UserID(generate_uuid())

BRAND = Brand(
    id=BrandID('test-brand'),
    title='Test Brand',
    image_filename=None,
    image_url_path=None,
    archived=False,
)

SENDER = NameAndAddress(name='Test Brand', address='noreply@example.com')

EMAIL_CONFIG = EmailConfig(
    brand_id=BrandID('test-brand'),
    sender=SENDER,
    contact_address='info@example.com',
)

# Match-ready snippet content, mirroring test_notification_service.py.
BODY_TEMPLATE = (
    '{tournament_name} round {match_round}: {opponent_name} at '
    '{opponent_seat}. Your seat: {your_seat}\n\n{footer}'
)
SUBJECT_TEMPLATE = '[{tournament_name}] Round {match_round} ready'

# Request-decision snippet content used by the mocked snippet lookups.
ACCEPTED_BODY_TEMPLATE = (
    'Your request "{request_name}" was accepted.\n\n{footer}'
)
ACCEPTED_SUBJECT_TEMPLATE = '[{request_name}] accepted'
REJECTED_BODY_TEMPLATE = (
    'Your request "{request_name}" was rejected: {reason}\n\n{footer}'
)
REJECTED_SUBJECT_TEMPLATE = '[{request_name}] rejected'


def _fake_snippet(name: str, language_code: str) -> SimpleNamespace:
    """A stand-in for `DbSnippet`, exposing only what `_find_missing_snippets`
    reads off it.
    """
    return SimpleNamespace(name=name, language_code=language_code)


def _snippet_side_effect(scope, name, language_code):
    """Return the fixed template body for any of the six known snippet names."""
    templates = {
        SNIPPET_NAME_BODY: BODY_TEMPLATE,
        SNIPPET_NAME_SUBJECT: SUBJECT_TEMPLATE,
        SNIPPET_NAME_REQUEST_ACCEPTED_BODY: ACCEPTED_BODY_TEMPLATE,
        SNIPPET_NAME_REQUEST_ACCEPTED_SUBJECT: ACCEPTED_SUBJECT_TEMPLATE,
        SNIPPET_NAME_REQUEST_REJECTED_BODY: REJECTED_BODY_TEMPLATE,
        SNIPPET_NAME_REQUEST_REJECTED_SUBJECT: REJECTED_SUBJECT_TEMPLATE,
    }
    if name in templates:
        return Ok(templates[name])
    return Err(f'Unknown snippet: {name}')


def _make_request(
    *,
    name='CS2 Community Cup',
    proposer_id=None,
    status=TournamentRequestStatus.accepted,
    rejection_reason=None,
) -> TournamentRequest:
    return TournamentRequest(
        id=TournamentRequestID(generate_uuid()),
        party_id=PARTY_ID,
        number=1,
        proposer_id=proposer_id or PROPOSER_ID,
        created_at=NOW,
        updated_at=None,
        status=status,
        name=name,
        game='CS2',
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        team_size=1,
        participant_limit=16,
        preferred_start_time=NOW,
        preferred_end_time=NOW,
        description='A description',
        special_rules=None,
        notes=None,
        desired_template=None,
        decided_at=None,
        decided_by_id=None,
        rejection_reason=rejection_reason,
        created_tournament_id=None,
    )


# All patches target the module under test's imported references.
MODULE = 'byceps.services.lan_tournament.tournament_notification_service'
MODULE_HANDLERS = 'byceps.services.lan_tournament.notification_handlers'


# --------------------------------------------------------------------- #
# create_tournament_request_email_snippets
# --------------------------------------------------------------------- #


@patch(f'{MODULE}.snippet_service')
def test_create_request_snippets_is_idempotent(mock_snippet_service):
    """When every request snippet already exists, nothing is created."""
    request_pairs = [
        (SNIPPET_NAME_REQUEST_ACCEPTED_BODY, 'en'),
        (SNIPPET_NAME_REQUEST_ACCEPTED_BODY, 'de'),
        (SNIPPET_NAME_REQUEST_ACCEPTED_SUBJECT, 'en'),
        (SNIPPET_NAME_REQUEST_ACCEPTED_SUBJECT, 'de'),
        (SNIPPET_NAME_REQUEST_REJECTED_BODY, 'en'),
        (SNIPPET_NAME_REQUEST_REJECTED_BODY, 'de'),
        (SNIPPET_NAME_REQUEST_REJECTED_SUBJECT, 'en'),
        (SNIPPET_NAME_REQUEST_REJECTED_SUBJECT, 'de'),
    ]
    mock_snippet_service.get_snippets_for_scope_with_current_versions.return_value = [
        _fake_snippet(name, language_code)
        for name, language_code in request_pairs
    ]

    creator = MagicMock(name='creator')
    created = create_tournament_request_email_snippets(BRAND, creator)

    assert created is False
    mock_snippet_service.create_snippet.assert_not_called()


@patch(f'{MODULE}.snippet_service')
def test_create_request_snippets_creates_en_and_de_pairs(mock_snippet_service):
    """When nothing exists yet, all 4 request names are created in en+de."""
    mock_snippet_service.find_current_version_of_snippet_with_name.return_value = None

    creator = MagicMock(name='creator')
    created = create_tournament_request_email_snippets(BRAND, creator)

    assert created is True
    assert mock_snippet_service.create_snippet.call_count == 8

    created_pairs = {
        (call.args[1], call.args[2])
        for call in mock_snippet_service.create_snippet.call_args_list
    }
    expected_pairs = {
        (SNIPPET_NAME_REQUEST_ACCEPTED_BODY, 'en'),
        (SNIPPET_NAME_REQUEST_ACCEPTED_BODY, 'de'),
        (SNIPPET_NAME_REQUEST_ACCEPTED_SUBJECT, 'en'),
        (SNIPPET_NAME_REQUEST_ACCEPTED_SUBJECT, 'de'),
        (SNIPPET_NAME_REQUEST_REJECTED_BODY, 'en'),
        (SNIPPET_NAME_REQUEST_REJECTED_BODY, 'de'),
        (SNIPPET_NAME_REQUEST_REJECTED_SUBJECT, 'en'),
        (SNIPPET_NAME_REQUEST_REJECTED_SUBJECT, 'de'),
    }
    assert created_pairs == expected_pairs


# --------------------------------------------------------------------- #
# send_request_accepted_email / send_request_rejected_email
# --------------------------------------------------------------------- #


@patch(f'{MODULE}.email_service')
@patch(f'{MODULE}.email_footer_service')
@patch(f'{MODULE}.snippet_service')
@patch(f'{MODULE}.user_service')
@patch(f'{MODULE}.email_config_service')
@patch(f'{MODULE}.get_default_locale')
def test_rejected_email_includes_reason(
    mock_default_locale,
    mock_config_service,
    mock_user_service,
    mock_snippet_service,
    mock_footer_service,
    mock_email_service,
):
    """The rejected-request email body includes the request name and reason."""
    mock_default_locale.return_value = Locale('en')
    mock_config_service.get_config.return_value = EMAIL_CONFIG
    mock_user_service.find_email_address.return_value = 'proposer@example.com'
    mock_user_service.find_locale.return_value = None
    mock_snippet_service.get_snippet_body.side_effect = _snippet_side_effect
    mock_footer_service.get_footer.return_value = Ok('-- Footer')

    request = _make_request(
        name='CS2 Community Cup',
        status=TournamentRequestStatus.rejected,
        rejection_reason='Venue is double-booked that weekend.',
    )
    send_request_rejected_email(BRAND, request)

    assert mock_email_service.enqueue_message.call_count == 1
    message = mock_email_service.enqueue_message.call_args[0][0]
    assert message.sender == SENDER
    assert message.recipients == ['proposer@example.com']
    assert 'CS2 Community Cup' in message.body
    assert 'Venue is double-booked that weekend.' in message.body


@patch(f'{MODULE}.log')
@patch(f'{MODULE}.email_service')
@patch(f'{MODULE}.snippet_service')
@patch(f'{MODULE}.user_service')
@patch(f'{MODULE}.email_config_service')
def test_email_skipped_when_proposer_has_no_address(
    mock_config_service,
    mock_user_service,
    mock_snippet_service,
    mock_email_service,
    mock_log,
):
    """A proposer with no usable address is skipped, never raised on."""
    mock_config_service.get_config.return_value = EMAIL_CONFIG
    mock_user_service.find_email_address.return_value = None

    request = _make_request()
    send_request_accepted_email(BRAND, request)

    mock_email_service.enqueue_message.assert_not_called()
    mock_snippet_service.get_snippet_body.assert_not_called()
    mock_log.warning.assert_called_once()


@patch(f'{MODULE}.email_service')
@patch(f'{MODULE}.email_footer_service')
@patch(f'{MODULE}.snippet_service')
@patch(f'{MODULE}.user_service')
@patch(f'{MODULE}.email_config_service')
@patch(f'{MODULE}.get_default_locale')
def test_request_email_values_are_brace_escaped(
    mock_default_locale,
    mock_config_service,
    mock_user_service,
    mock_snippet_service,
    mock_footer_service,
    mock_email_service,
):
    """A request name or reason containing `{0}`/`{__class__}` renders
    as inert, doubled-brace text and never raises (format-string
    injection guard). `str.format()` never re-expands a substituted
    *value*, so the escaped braces stay doubled in the final email
    rather than collapsing back to single braces -- that is the same
    (unchanged) behaviour as the match-ready path for a brace-bearing
    opponent name.
    """
    mock_default_locale.return_value = Locale('en')
    mock_config_service.get_config.return_value = EMAIL_CONFIG
    mock_user_service.find_email_address.return_value = 'proposer@example.com'
    mock_user_service.find_locale.return_value = None
    mock_snippet_service.get_snippet_body.side_effect = _snippet_side_effect
    mock_footer_service.get_footer.return_value = Ok('-- Footer')

    request = _make_request(
        name='Cup {0}',
        status=TournamentRequestStatus.rejected,
        rejection_reason='Reason {__class__.__mro__}',
    )

    send_request_rejected_email(BRAND, request)

    assert mock_email_service.enqueue_message.call_count == 1
    message = mock_email_service.enqueue_message.call_args[0][0]
    assert 'Cup {{0}}' in message.body
    assert 'Reason {{__class__.__mro__}}' in message.body
    # Never raised into an attribute/index traversal on `{0.__class__}`.
    assert '.__mro__[' not in message.body


# --------------------------------------------------------------------- #
# accepted-email default text -- no promise of a follow-up that never comes
# --------------------------------------------------------------------- #


@patch(f'{MODULE}.snippet_service')
def test_accepted_body_default_text_promises_no_unsent_followup(
    mock_snippet_service,
):
    """The default accepted-request body no longer says staff will
    'let you know'/'meldet sich' once the tournament is ready -- no
    such notification is ever sent. It points the proposer at the
    tournament-requests page instead, which they can check themselves.
    """
    mock_snippet_service.get_snippets_for_scope_with_current_versions.return_value = []

    creator = MagicMock(name='creator')
    create_tournament_request_email_snippets(BRAND, creator)

    bodies = {
        (call.args[1], call.args[2]): call.args[4]
        for call in mock_snippet_service.create_snippet.call_args_list
    }
    en_body = bodies[(SNIPPET_NAME_REQUEST_ACCEPTED_BODY, 'en')]
    de_body = bodies[(SNIPPET_NAME_REQUEST_ACCEPTED_BODY, 'de')]

    assert 'let you know' not in en_body
    assert 'meldet sich' not in de_body
    assert 'tournament requests' in en_body.lower()
    assert 'turnieranfragen' in de_body.lower()
    # Placeholders are unchanged.
    assert '{request_name}' in en_body
    assert '{footer}' in en_body
    assert '{request_name}' in de_body
    assert '{footer}' in de_body


# --------------------------------------------------------------------- #
# language fallback -- item s
# --------------------------------------------------------------------- #


def _snippet_body_only_in(available_language_codes: set[str]):
    """A `snippet_service.get_snippet_body` side effect that only has
    the rejected-request templates for the given languages.
    """

    def side_effect(scope, name, language_code):
        if language_code not in available_language_codes:
            return Err(f'No snippet {name} for {language_code}')
        if name == SNIPPET_NAME_REQUEST_REJECTED_BODY:
            return Ok(REJECTED_BODY_TEMPLATE)
        if name == SNIPPET_NAME_REQUEST_REJECTED_SUBJECT:
            return Ok(REJECTED_SUBJECT_TEMPLATE)
        return Err(f'Unknown snippet: {name}')

    return side_effect


def _footer_only_in(available_language_codes: set[str]):
    def side_effect(brand, language_code):
        if language_code in available_language_codes:
            return Ok('-- Footer')
        return Err('No footer for that language')

    return side_effect


@patch(f'{MODULE}.email_service')
@patch(f'{MODULE}.email_footer_service')
@patch(f'{MODULE}.snippet_service')
@patch(f'{MODULE}.user_service')
@patch(f'{MODULE}.email_config_service')
@patch(f'{MODULE}.get_default_locale')
def test_decision_email_fr_user_falls_back_to_en_default(
    mock_default_locale,
    mock_config_service,
    mock_user_service,
    mock_snippet_service,
    mock_footer_service,
    mock_email_service,
):
    """A French-locale recipient with only `en` snippets gets the
    email in `en` (the app default) instead of nothing.
    """
    mock_default_locale.return_value = Locale('en')
    mock_config_service.get_config.return_value = EMAIL_CONFIG
    mock_user_service.find_email_address.return_value = 'proposer@example.com'
    mock_user_service.find_locale.return_value = Locale('fr')
    mock_snippet_service.get_snippet_body.side_effect = (
        _snippet_body_only_in({'en'})
    )
    mock_footer_service.get_footer.side_effect = _footer_only_in({'en'})

    request = _make_request(status=TournamentRequestStatus.rejected)
    send_request_rejected_email(BRAND, request)

    assert mock_email_service.enqueue_message.call_count == 1
    message = mock_email_service.enqueue_message.call_args[0][0]
    assert 'CS2 Community Cup' in message.body


@patch(f'{MODULE}.email_service')
@patch(f'{MODULE}.email_footer_service')
@patch(f'{MODULE}.snippet_service')
@patch(f'{MODULE}.user_service')
@patch(f'{MODULE}.email_config_service')
@patch(f'{MODULE}.get_default_locale')
def test_decision_email_de_user_falls_back_to_en_default(
    mock_default_locale,
    mock_config_service,
    mock_user_service,
    mock_snippet_service,
    mock_footer_service,
    mock_email_service,
):
    """A German-locale recipient with only `en` snippets (e.g. a brand
    whose de snippet has not been set up yet) also falls back to the
    app default rather than being silently skipped.
    """
    mock_default_locale.return_value = Locale('en')
    mock_config_service.get_config.return_value = EMAIL_CONFIG
    mock_user_service.find_email_address.return_value = 'proposer@example.com'
    mock_user_service.find_locale.return_value = Locale('de')
    mock_snippet_service.get_snippet_body.side_effect = (
        _snippet_body_only_in({'en'})
    )
    mock_footer_service.get_footer.side_effect = _footer_only_in({'en'})

    request = _make_request(status=TournamentRequestStatus.rejected)
    send_request_rejected_email(BRAND, request)

    assert mock_email_service.enqueue_message.call_count == 1
    message = mock_email_service.enqueue_message.call_args[0][0]
    assert 'CS2 Community Cup' in message.body


@patch(f'{MODULE}.log')
@patch(f'{MODULE}.email_service')
@patch(f'{MODULE}.email_footer_service')
@patch(f'{MODULE}.snippet_service')
@patch(f'{MODULE}.user_service')
@patch(f'{MODULE}.email_config_service')
@patch(f'{MODULE}.get_default_locale')
def test_decision_email_no_snippets_in_any_language_sends_nothing(
    mock_default_locale,
    mock_config_service,
    mock_user_service,
    mock_snippet_service,
    mock_footer_service,
    mock_email_service,
    mock_log,
):
    """Neither the recipient's language nor the app default has the
    snippets: today's error is logged and nothing is sent -- no crash,
    no silent partial email.
    """
    mock_default_locale.return_value = Locale('en')
    mock_config_service.get_config.return_value = EMAIL_CONFIG
    mock_user_service.find_email_address.return_value = 'proposer@example.com'
    mock_user_service.find_locale.return_value = Locale('fr')
    mock_snippet_service.get_snippet_body.side_effect = (
        _snippet_body_only_in(set())
    )
    mock_footer_service.get_footer.side_effect = _footer_only_in(set())

    request = _make_request(status=TournamentRequestStatus.rejected)
    send_request_rejected_email(BRAND, request)

    mock_email_service.enqueue_message.assert_not_called()
    mock_log.error.assert_called()


@patch(f'{MODULE}.email_service')
@patch(f'{MODULE}.email_footer_service')
@patch(f'{MODULE}.snippet_service')
@patch(f'{MODULE}.user_service')
@patch(f'{MODULE}.email_config_service')
@patch(f'{MODULE}.get_default_locale')
def test_decision_email_never_mixes_languages_within_one_email(
    mock_default_locale,
    mock_config_service,
    mock_user_service,
    mock_snippet_service,
    mock_footer_service,
    mock_email_service,
):
    """The recipient's own language (de) has a body snippet but not a
    subject snippet -- the whole email must fall back to the default
    language rather than mixing a de body with an en subject.
    """
    mock_default_locale.return_value = Locale('en')
    mock_config_service.get_config.return_value = EMAIL_CONFIG
    mock_user_service.find_email_address.return_value = 'proposer@example.com'
    mock_user_service.find_locale.return_value = Locale('de')

    de_only_body = 'DE-ONLY BODY {request_name}: {reason}\n{footer}'

    def snippet_side_effect(scope, name, language_code):
        if name == SNIPPET_NAME_REQUEST_REJECTED_BODY:
            if language_code == 'de':
                return Ok(de_only_body)
            if language_code == 'en':
                return Ok(REJECTED_BODY_TEMPLATE)
            return Err('missing')
        if name == SNIPPET_NAME_REQUEST_REJECTED_SUBJECT:
            if language_code == 'en':
                return Ok(REJECTED_SUBJECT_TEMPLATE)
            return Err('missing')  # no de subject
        return Err(f'Unknown snippet: {name}')

    mock_snippet_service.get_snippet_body.side_effect = snippet_side_effect
    mock_footer_service.get_footer.side_effect = _footer_only_in({'en'})

    request = _make_request(status=TournamentRequestStatus.rejected)
    send_request_rejected_email(BRAND, request)

    assert mock_email_service.enqueue_message.call_count == 1
    message = mock_email_service.enqueue_message.call_args[0][0]
    # Must be the EN body, not the DE one -- proves the de body wasn't
    # mixed with the en subject/footer.
    assert 'DE-ONLY BODY' not in message.body
    assert 'CS2 Community Cup' in message.body


# --------------------------------------------------------------------- #
# match-ready guard -- proves the helper extraction changed nothing
# --------------------------------------------------------------------- #


@patch(f'{MODULE}.email_service')
@patch(f'{MODULE}.email_footer_service')
@patch(f'{MODULE}.snippet_service')
@patch(f'{MODULE}.user_service')
@patch(f'{MODULE}.get_default_locale')
def test_match_ready_email_unchanged_after_helper_extraction(
    mock_default_locale,
    mock_user_service,
    mock_snippet_service,
    mock_footer_service,
    mock_email_service,
):
    """Exact sender/recipient/subject/body for a fixed match-ready fixture.

    This pins down `_send_email_to_user`'s observable behaviour so a
    broken extraction of the shared tail (e.g. a dropped footer) fails
    this test. See guardrails for the mutation-test result.
    """
    from byceps.services.lan_tournament import (
        tournament_notification_service as notif,
    )

    user_id = UserID(generate_uuid())

    mock_default_locale.return_value = Locale('en')
    mock_user_service.find_email_address.return_value = 'alice@example.com'
    mock_user_service.find_locale.return_value = None
    mock_snippet_service.get_snippet_body.side_effect = _snippet_side_effect
    mock_footer_service.get_footer.return_value = Ok('-- Test Footer')

    notif._send_email_to_user(
        user_id=user_id,
        sender=SENDER,
        brand=BRAND,
        tournament_name='CS2 Cup',
        match_round=3,
        opponent_name='Bob',
        opponent_seat='B2',
        your_seat='A1',
    )

    assert mock_email_service.enqueue_message.call_count == 1
    message = mock_email_service.enqueue_message.call_args[0][0]

    expected_body = BODY_TEMPLATE.format(
        tournament_name='CS2 Cup',
        match_round='3',
        opponent_name='Bob',
        opponent_seat='B2',
        your_seat='A1',
        footer='-- Test Footer',
    )
    expected_subject = SUBJECT_TEMPLATE.format(
        tournament_name='CS2 Cup',
        match_round='3',
        opponent_name='Bob',
        opponent_seat='B2',
        your_seat='A1',
        footer='-- Test Footer',
    )

    assert message.sender == SENDER
    assert message.recipients == ['alice@example.com']
    assert message.subject == expected_subject
    assert message.body == expected_body


# --------------------------------------------------------------------- #
# email_templates_exist / setup -- one source of truth for 12 pairs
# --------------------------------------------------------------------- #


@pytest.mark.parametrize('missing_pair', _REQUIRED_SNIPPET_PAIRS)
@patch(f'{MODULE}.snippet_service')
def test_email_templates_exist_requires_every_pair(
    mock_snippet_service, missing_pair
):
    """With exactly one of the 12 pairs missing, the brand is not configured."""
    existing = [
        _fake_snippet(name, language_code)
        for name, language_code in _REQUIRED_SNIPPET_PAIRS
        if (name, language_code) != missing_pair
    ]
    mock_snippet_service.get_snippets_for_scope_with_current_versions.return_value = existing

    assert email_templates_exist(BRAND) is False


@patch(f'{MODULE}.snippet_service')
def test_email_templates_exist_true_when_all_pairs_present(
    mock_snippet_service,
):
    """With all 12 pairs present, the brand is fully configured."""
    existing = [
        _fake_snippet(name, language_code)
        for name, language_code in _REQUIRED_SNIPPET_PAIRS
    ]
    mock_snippet_service.get_snippets_for_scope_with_current_versions.return_value = existing

    assert email_templates_exist(BRAND) is True


@patch(f'{MODULE}.snippet_service')
def test_setup_creates_only_missing_snippets(mock_snippet_service):
    """Only the one missing pair is created; existing ones are untouched."""
    missing_pair = (SNIPPET_NAME_REQUEST_REJECTED_SUBJECT, 'de')
    existing = [
        _fake_snippet(name, language_code)
        for name, language_code in _REQUIRED_SNIPPET_PAIRS
        if (name, language_code) != missing_pair
    ]
    mock_snippet_service.get_snippets_for_scope_with_current_versions.return_value = existing

    creator = MagicMock(name='creator')
    created_match_ready = create_match_ready_email_snippets(BRAND, creator)
    created_request = create_tournament_request_email_snippets(BRAND, creator)

    assert created_match_ready or created_request
    assert mock_snippet_service.create_snippet.call_count == 1

    call = mock_snippet_service.create_snippet.call_args
    assert call.args[1] == SNIPPET_NAME_REQUEST_REJECTED_SUBJECT
    assert call.args[2] == 'de'


@patch(f'{MODULE}.snippet_service')
def test_find_missing_snippets_uses_one_bulk_fetch_not_per_pair_lookups(
    mock_snippet_service,
):
    """The missing set is computed from a single scope-wide fetch; the
    per-name finder (12 queries on every admin overview render) is
    never called.
    """
    from byceps.services.lan_tournament.tournament_notification_service import (
        _find_missing_snippets,
    )

    mock_snippet_service.get_snippets_for_scope_with_current_versions.return_value = [
        _fake_snippet(SNIPPET_NAME_BODY, 'en'),
    ]

    missing = _find_missing_snippets(BRAND, _REQUIRED_SNIPPET_PAIRS)

    assert (SNIPPET_NAME_BODY, 'en') not in missing
    assert (SNIPPET_NAME_BODY, 'de') in missing
    mock_snippet_service.get_snippets_for_scope_with_current_versions.assert_called_once_with(
        SnippetScope.for_brand(BRAND.id)
    )
    mock_snippet_service.find_current_version_of_snippet_with_name.assert_not_called()


@patch(f'{MODULE}.db')
@patch(f'{MODULE}.snippet_service')
def test_create_missing_snippets_survives_concurrent_race(
    mock_snippet_service, mock_db
):
    """Two concurrent setups racing on the same pair: the loser's
    `IntegrityError` is swallowed (after a rollback) instead of
    propagating as a 500, and the other missing pairs are still
    created.
    """
    from byceps.services.lan_tournament.tournament_notification_service import (
        _create_missing_snippets,
    )

    raced_pair = (SNIPPET_NAME_BODY, 'en')
    other_pair = (SNIPPET_NAME_BODY, 'de')

    mock_snippet_service.get_snippets_for_scope_with_current_versions.return_value = []

    def create_side_effect(scope, name, language_code, creator, body):
        if (name, language_code) == raced_pair:
            raise IntegrityError('INSERT', {}, Exception('duplicate key'))
        return MagicMock()

    mock_snippet_service.create_snippet.side_effect = create_side_effect
    # After the rollback, the re-check finds the pair now exists
    # (a concurrent setup won the race).
    mock_snippet_service.find_current_version_of_snippet_with_name.return_value = (
        MagicMock()
    )

    creator = MagicMock(name='creator')
    created = _create_missing_snippets(
        BRAND,
        creator,
        {raced_pair: 'body-en', other_pair: 'body-de'},
    )

    assert created is True  # the other pair was created by this call
    assert mock_snippet_service.create_snippet.call_count == 2
    mock_db.session.rollback.assert_called_once()
    mock_snippet_service.find_current_version_of_snippet_with_name.assert_called_once_with(
        SnippetScope.for_brand(BRAND.id), *raced_pair
    )


@patch(f'{MODULE}.db')
@patch(f'{MODULE}.snippet_service')
def test_create_missing_snippets_reraises_when_recheck_still_missing(
    mock_snippet_service, mock_db
):
    """An `IntegrityError` that is not a concurrent-setup race (e.g. a
    FK violation from an unknown language code) must not be swallowed
    as though it were one. The recheck confirms the pair is still
    missing, so the original exception propagates instead of being
    logged and hidden behind a truthy-looking return value.
    """
    from byceps.services.lan_tournament.tournament_notification_service import (
        _create_missing_snippets,
    )

    bad_pair = (SNIPPET_NAME_BODY, 'en')

    mock_snippet_service.get_snippets_for_scope_with_current_versions.return_value = []

    def create_side_effect(scope, name, language_code, creator, body):
        raise IntegrityError('INSERT', {}, Exception('fk violation'))

    mock_snippet_service.create_snippet.side_effect = create_side_effect
    # After the rollback, the re-check still finds nothing -- not a race.
    mock_snippet_service.find_current_version_of_snippet_with_name.return_value = None

    creator = MagicMock(name='creator')

    with pytest.raises(IntegrityError):
        _create_missing_snippets(BRAND, creator, {bad_pair: 'body-en'})

    mock_db.session.rollback.assert_called_once()
    mock_snippet_service.find_current_version_of_snippet_with_name.assert_called_once_with(
        SnippetScope.for_brand(BRAND.id), *bad_pair
    )


@patch(f'{MODULE}._create_missing_snippets')
def test_create_functions_content_dicts_cover_every_required_pair(
    mock_create_missing_snippets,
):
    """The two `create_*_email_snippets` functions' hand-written
    `contents_by_pair` dicts are not derived from `_REQUIRED_SNIPPET_PAIRS`
    (see the comment above that tuple) -- pin that, taken together, they
    still cover it exactly: no pair `email_templates_exist` requires is
    left with no `create_*` function to supply it, and neither dict
    claims a pair the other also claims.
    """
    mock_create_missing_snippets.return_value = False
    creator = MagicMock(name='creator')

    create_match_ready_email_snippets(BRAND, creator)
    match_ready_pairs = set(mock_create_missing_snippets.call_args.args[2])

    create_tournament_request_email_snippets(BRAND, creator)
    request_pairs = set(mock_create_missing_snippets.call_args.args[2])

    assert match_ready_pairs | request_pairs == set(_REQUIRED_SNIPPET_PAIRS)
    assert match_ready_pairs.isdisjoint(request_pairs)


# --------------------------------------------------------------------- #
# setup notice/button -- overview.html fragment
# --------------------------------------------------------------------- #


_OVERVIEW_TEMPLATE = pathlib.Path(
    'byceps/services/lan_tournament/blueprints/admin/templates'
    '/admin/lan_tournament/overview.html'
)


@pytest.fixture(scope='module')
def setup_notice_env():
    src = _OVERVIEW_TEMPLATE.read_text()
    start = src.index('{# Email template setup #}')
    end = src.index('{# Tournament list #}', start)
    fragment = src[start:end]

    template = "{% from 'macros/icons.html' import render_icon %}\n" + fragment

    e = Environment(
        undefined=StrictUndefined,
        autoescape=True,
        loader=DictLoader(
            {
                'setup_notice': template,
                'macros/icons.html': (
                    '{% macro render_icon(name, color=None, title=None, '
                    "filename='icons') %}{% endmacro %}"
                ),
            }
        ),
    )
    e.globals['_'] = lambda s, **kw: (s % kw) if kw else s
    e.globals['url_for'] = lambda endpoint, **k: '/' + endpoint.lstrip('.')
    return e


def _render_setup_notice(env, *, configured, can_administrate=True):
    tmpl = env.get_template('setup_notice')
    g = SimpleNamespace(
        user=SimpleNamespace(
            has_permission=lambda perm: (
                can_administrate and perm == 'lan_tournament.administrate'
            )
        )
    )
    return tmpl.render(
        g=g,
        party=SimpleNamespace(id='p1'),
        email_templates_configured=configured,
    )


def test_setup_button_hidden_after_setup(setup_notice_env):
    """The setup form is present while unconfigured, gone once complete."""
    before = _render_setup_notice(setup_notice_env, configured=False)
    assert (
        '<form action="/setup_email_templates_for_party" method="post">'
        in before
    )

    after = _render_setup_notice(setup_notice_env, configured=True)
    assert (
        '<form action="/setup_email_templates_for_party" method="post">'
        not in after
    )


# --------------------------------------------------------------------- #
# notification_handlers -- signal wiring
# --------------------------------------------------------------------- #


def _make_accepted_event(*, request_id=None, party_id=None):
    return TournamentRequestAcceptedEvent(
        occurred_at=NOW_UTC,
        initiator=None,
        request_id=request_id or TournamentRequestID(generate_uuid()),
        party_id=party_id or PARTY_ID,
        proposer_id=PROPOSER_ID,
        decided_by_id=UserID(generate_uuid()),
    )


def _make_rejected_event(*, request_id=None, party_id=None):
    return TournamentRequestRejectedEvent(
        occurred_at=NOW_UTC,
        initiator=None,
        request_id=request_id or TournamentRequestID(generate_uuid()),
        party_id=party_id or PARTY_ID,
        proposer_id=PROPOSER_ID,
        decided_by_id=UserID(generate_uuid()),
        reason='Venue unavailable',
    )


@patch(f'{MODULE_HANDLERS}.tournament_notification_service')
@patch(f'{MODULE_HANDLERS}.brand_service')
@patch(f'{MODULE_HANDLERS}.party_service')
@patch(f'{MODULE_HANDLERS}.tournament_request_repository')
def test_accepted_email_dispatched_on_signal(
    mock_repo, mock_party_service, mock_brand_service, mock_notif
):
    """Firing the real `tournament_request_accepted` signal reaches the
    notification service, proving `enable_match_notifications()`'s wiring.
    """
    request = _make_request()
    party = SimpleNamespace(brand_id=BrandID('test-brand'))

    mock_repo.find_request.return_value = request
    mock_party_service.get_party.return_value = party
    mock_brand_service.get_brand.return_value = BRAND

    event = _make_accepted_event(request_id=request.id)

    tournament_request_accepted.connect(_on_tournament_request_accepted)
    try:
        tournament_request_accepted.send(None, event=event)
    finally:
        tournament_request_accepted.disconnect(_on_tournament_request_accepted)

    mock_repo.find_request.assert_called_once_with(event.request_id)
    mock_party_service.get_party.assert_called_once_with(event.party_id)
    mock_brand_service.get_brand.assert_called_once_with(party.brand_id)
    mock_notif.send_request_accepted_email.assert_called_once_with(
        BRAND,
        request,
    )


@patch(f'{MODULE_HANDLERS}.tournament_notification_service')
@patch(f'{MODULE_HANDLERS}.brand_service')
@patch(f'{MODULE_HANDLERS}.party_service')
@patch(f'{MODULE_HANDLERS}.tournament_request_repository')
def test_rejected_email_dispatched_on_signal(
    mock_repo, mock_party_service, mock_brand_service, mock_notif
):
    """Same wiring check for the rejected-decision signal."""
    request = _make_request(status=TournamentRequestStatus.rejected)
    party = SimpleNamespace(brand_id=BrandID('test-brand'))

    mock_repo.find_request.return_value = request
    mock_party_service.get_party.return_value = party
    mock_brand_service.get_brand.return_value = BRAND

    event = _make_rejected_event(request_id=request.id)

    tournament_request_rejected.connect(_on_tournament_request_rejected)
    try:
        tournament_request_rejected.send(None, event=event)
    finally:
        tournament_request_rejected.disconnect(_on_tournament_request_rejected)

    mock_notif.send_request_rejected_email.assert_called_once_with(
        BRAND,
        request,
    )


@patch(f'{MODULE_HANDLERS}.log')
@patch(f'{MODULE_HANDLERS}.tournament_notification_service')
@patch(f'{MODULE_HANDLERS}.brand_service')
@patch(f'{MODULE_HANDLERS}.party_service')
@patch(f'{MODULE_HANDLERS}.tournament_request_repository')
def test_handler_swallows_send_failure(
    mock_repo, mock_party_service, mock_brand_service, mock_notif, mock_log
):
    """A raising send function never propagates out of the handler."""
    request = _make_request()
    mock_repo.find_request.return_value = request
    mock_party_service.get_party.return_value = SimpleNamespace(
        brand_id=BrandID('test-brand')
    )
    mock_brand_service.get_brand.return_value = BRAND
    mock_notif.send_request_accepted_email.side_effect = RuntimeError(
        'SMTP down'
    )

    event = _make_accepted_event(request_id=request.id)

    # Must not raise.
    _on_tournament_request_accepted(None, event=event)

    mock_log.exception.assert_called_once()
