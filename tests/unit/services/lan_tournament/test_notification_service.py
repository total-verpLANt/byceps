"""
tests.unit.services.lan_tournament.test_notification_service
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Unit tests for the match-ready message tail and snippet setup.
"""

from datetime import datetime
from unittest.mock import patch

import pytest
from babel import Locale

from byceps.services.brand.models import Brand, BrandID
from byceps.services.email.models import EmailConfig, NameAndAddress
from byceps.services.lan_tournament.models.contestant_type import (
    ContestantType,
)
from byceps.services.lan_tournament.models.tournament import (
    Tournament,
    TournamentID,
)
from byceps.services.lan_tournament.models.tournament_match import (
    TournamentMatch,
    TournamentMatchID,
)
from byceps.services.party.models import Party, PartyID
from byceps.services.user.models import User, UserID
from byceps.util.result import Err, Ok

from tests.helpers import generate_uuid


NOW = datetime(2025, 6, 15, 14, 0, 0)

SUBJECT_TEMPLATE = '[{tournament_name}] Round {match_round} ready'

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

PARTY = Party(
    id=PartyID('test-party-2025'),
    brand_id=BrandID('test-brand'),
    title='Test Party 2025',
    starts_at=NOW,
    ends_at=NOW,
    max_ticket_quantity=None,
    ticket_management_enabled=True,
    seat_management_enabled=True,
    hidden=False,
    canceled=False,
    archived=False,
)

TOURNAMENT_ID = TournamentID(generate_uuid())
MATCH_ID = TournamentMatchID(generate_uuid())


def _make_tournament(
    *,
    contestant_type: ContestantType = ContestantType.SOLO,
) -> Tournament:
    return Tournament(
        id=TOURNAMENT_ID,
        party_id=PARTY.id,
        name='CS2 Cup',
        game='CS2',
        description=None,
        image_url=None,
        ruleset=None,
        start_time=None,
        created_at=NOW,
        min_players=None,
        max_players=None,
        min_teams=None,
        max_teams=None,
        min_players_in_team=None,
        max_players_in_team=None,
        contestant_type=contestant_type,
        tournament_status=None,
        game_format=None,
        elimination_mode=None,
    )


def _make_match() -> TournamentMatch:
    return TournamentMatch(
        id=MATCH_ID,
        tournament_id=TOURNAMENT_ID,
        group_order=None,
        match_order=1,
        round=1,
        next_match_id=None,
        confirmed_by=None,
        created_at=NOW,
    )


def _make_user(user_id: UserID, screen_name: str) -> User:
    return User(
        id=user_id,
        screen_name=screen_name,
        initialized=True,
        suspended=False,
        deleted=False,
        avatar_url='/static/user_avatar_fallback.svg',
    )


# All patches target the module under test's imported references.
MODULE = 'byceps.services.lan_tournament.tournament_notification_service'


@pytest.mark.parametrize(
    'bad_body_template',
    [
        pytest.param('{0}', id='IndexError'),
        pytest.param('{tournament_name.x}', id='AttributeError'),
        pytest.param('{tournament_name[a]}', id='TypeError'),
    ],
)
@patch(f'{MODULE}.log')
@patch(f'{MODULE}.email_service')
@patch(f'{MODULE}.email_footer_service')
@patch(f'{MODULE}.snippet_service')
@patch(f'{MODULE}.user_service')
@patch(f'{MODULE}.get_default_locale')
def test_malformed_snippet_is_caught_not_raised_for_every_error_type(
    mock_default_locale,
    mock_user_service,
    mock_snippet_service,
    mock_footer_service,
    mock_email_service,
    mock_log,
    bad_body_template,
):
    """A body snippet raising IndexError/AttributeError/TypeError out of
    `.format()` is logged and returned as an error, never raised
    (previously only KeyError/ValueError were caught).
    """
    from byceps.services.lan_tournament import (
        tournament_notification_service as notif,
    )

    user_id = UserID(generate_uuid())

    mock_default_locale.return_value = Locale('en')
    mock_user_service.find_email_address.return_value = 'alice@example.com'
    mock_user_service.find_locale.return_value = None

    def snippet_side_effect(scope, name, language_code):
        if name == 'email_match_ready_body':
            return Ok(bad_body_template)
        elif name == 'email_match_ready_subject':
            return Ok(SUBJECT_TEMPLATE)
        return Err(f'Unknown snippet: {name}')

    mock_snippet_service.get_snippet_body.side_effect = snippet_side_effect
    mock_footer_service.get_footer.return_value = Ok('-- Test Footer')

    # Must not raise.
    result = notif._build_match_ready_message(
        user_id=user_id,
        sender=SENDER,
        brand=BRAND,
        tournament_name='CS2 Cup',
        match_round=3,
        opponent_name='Bob',
        opponent_seat='B2',
        your_seat='A1',
    )

    assert result.is_err()
    assert result.unwrap_err() == 'email_template_formatting_failed'
    mock_email_service.enqueue_message.assert_not_called()
    mock_log.error.assert_called()


def test_admin_edited_snippets_are_not_overwritten():
    from types import SimpleNamespace

    from byceps.services.lan_tournament import tournament_notification_service as notif

    missing_pair = (notif.SNIPPET_NAME_SUBJECT, 'de')
    existing = [
        SimpleNamespace(name=name, language_code=language, body='Admin-edited text')
        for name, language in notif._REQUIRED_SNIPPET_PAIRS
        if (name, language) != missing_pair
    ]
    with patch(f'{MODULE}.snippet_service') as snippets:
        snippets.get_snippets_for_scope_with_current_versions.return_value = existing
        creator = _make_user(UserID(generate_uuid()), 'Admin')
        assert notif.create_match_ready_email_snippets(BRAND, creator) is True
        assert notif.create_tournament_request_email_snippets(BRAND, creator) is False
        assert snippets.create_snippet.call_count == 1
        assert snippets.create_snippet.call_args.args[1:3] == missing_pair
        snippets.update_snippet.assert_not_called()
        # After the one missing pair has been installed, repeat setup is a no-op.
        existing.append(SimpleNamespace(name=missing_pair[0], language_code=missing_pair[1]))
        snippets.create_snippet.reset_mock()
        assert notif.create_match_ready_email_snippets(BRAND, creator) is False
        assert notif.create_tournament_request_email_snippets(BRAND, creator) is False
        snippets.create_snippet.assert_not_called()


# fmt: off
@pytest.mark.parametrize('decision', ['accepted', 'rejected'])
@pytest.mark.parametrize('has_address', [True, False])
# fmt: on
def test_request_notification_contract_unchanged(decision, has_address):
    from byceps.services.lan_tournament import tournament_notification_service as notif
    from tests.unit.services.lan_tournament.test_tournament_request_notifications import _make_request

    request = _make_request(name='Community Cup', rejection_reason='No capacity')
    with (
        patch(f'{MODULE}.email_config_service.get_config', return_value=EMAIL_CONFIG),
        patch(f'{MODULE}.user_service.find_email_address', return_value=(
            'proposer@example.com' if has_address else None
        )),
        patch(f'{MODULE}.user_service.find_locale', return_value=Locale('de')),
        patch(f'{MODULE}.get_default_locale', return_value=Locale('en')),
        patch(f'{MODULE}.snippet_service.get_snippet_body') as snippets,
        patch(f'{MODULE}.email_footer_service.get_footer', return_value=Ok('de footer')),
        patch(f'{MODULE}.email_service.enqueue_message') as enqueue,
        patch(f'{MODULE}.email_service.send_email') as send,
    ):
        snippets.side_effect = lambda scope, name, language: Ok(
            'de {request_name}: {reason}\n{footer}'
            if decision == 'rejected' and name.endswith('_body')
            else 'de {request_name}\n{footer}' if name.endswith('_body')
            else 'de {request_name}'
        )
        helper = getattr(notif, f'send_request_{decision}_email')
        assert helper(BRAND, request) is None
        send.assert_not_called()
        if not has_address:
            enqueue.assert_not_called()
            snippets.assert_not_called()
            return
        enqueue.assert_called_once()
        message = enqueue.call_args.args[0]
        assert message.recipients == ['proposer@example.com']
        assert message.sender == SENDER
        assert message.subject == 'de Community Cup'
        expected = 'de Community Cup: No capacity' if decision == 'rejected' else 'de Community Cup'
        assert message.body == f'{expected}\nde footer'
        assert {call.args[1] for call in snippets.call_args_list} == {
            f'email_tournament_request_{decision}_body',
            f'email_tournament_request_{decision}_subject',
        }
        assert {call.args[2] for call in snippets.call_args_list} == {'de'}
