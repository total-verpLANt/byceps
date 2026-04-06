"""
tests.unit.services.lan_tournament.test_notification_service
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Unit tests for tournament match-ready email notifications.

:Copyright: 2014-2026 Jochen Kupperschmidt
:License: Revised BSD (see `LICENSE` file for details)
"""

from datetime import datetime
from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest
from babel import Locale

from byceps.services.brand.models import Brand, BrandID
from byceps.services.email.models import EmailConfig, Message, NameAndAddress
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
from byceps.services.lan_tournament.models.tournament_match_to_contestant import (
    TournamentMatchToContestant,
    TournamentMatchToContestantID,
)
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipant,
    TournamentParticipantID,
)
from byceps.services.lan_tournament.models.tournament_team import (
    TournamentTeam,
    TournamentTeamID,
)
from byceps.services.party.models import Party, PartyID
from byceps.services.user.models import User, UserID
from byceps.util.result import Err, Ok

from tests.helpers import generate_uuid


NOW = datetime(2025, 6, 15, 14, 0, 0)

BODY_TEMPLATE = '{tournament_name} round {match_round}: {opponent_name} at {opponent_seat}. Your seat: {your_seat}\n\n{footer}'
SUBJECT_TEMPLATE = '[{tournament_name}] Round {match_round} ready'


def _snippet_side_effect(scope, name, language_code):
    """Return different Ok values for body vs subject snippet lookups."""
    if name == 'email_match_ready_body':
        return Ok(BODY_TEMPLATE)
    elif name == 'email_match_ready_subject':
        return Ok(SUBJECT_TEMPLATE)
    return Err(f'Unknown snippet: {name}')

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


@patch(f'{MODULE}.email_service')
@patch(f'{MODULE}.email_footer_service')
@patch(f'{MODULE}.snippet_service')
@patch(f'{MODULE}.user_service')
@patch(f'{MODULE}.tournament_participant_service')
@patch(f'{MODULE}.tournament_repository')
@patch(f'{MODULE}.email_config_service')
@patch(f'{MODULE}.party_service')
@patch(f'{MODULE}.brand_service')
def test_send_match_ready_emails_solo_tournament(
    mock_brand_service,
    mock_party_service,
    mock_email_config_service,
    mock_repo,
    mock_participant_service,
    mock_user_service,
    mock_snippet_service,
    mock_footer_service,
    mock_email_service,
):
    """In a solo tournament, each participant gets a personalised email."""
    from byceps.services.lan_tournament import tournament_notification_service

    tournament = _make_tournament(contestant_type=ContestantType.SOLO)
    match = _make_match()

    user_a_id = UserID(generate_uuid())
    user_b_id = UserID(generate_uuid())
    participant_a_id = TournamentParticipantID(generate_uuid())
    participant_b_id = TournamentParticipantID(generate_uuid())

    user_a = _make_user(user_a_id, 'Alice')
    user_b = _make_user(user_b_id, 'Bob')

    participant_a = TournamentParticipant(
        id=participant_a_id,
        user_id=user_a_id,
        tournament_id=TOURNAMENT_ID,
        substitute_player=False,
        team_id=None,
        created_at=NOW,
    )
    participant_b = TournamentParticipant(
        id=participant_b_id,
        user_id=user_b_id,
        tournament_id=TOURNAMENT_ID,
        substitute_player=False,
        team_id=None,
        created_at=NOW,
    )

    contestant_a = TournamentMatchToContestant(
        id=TournamentMatchToContestantID(generate_uuid()),
        tournament_match_id=MATCH_ID,
        team_id=None,
        participant_id=participant_a_id,
        score=None,
        created_at=NOW,
    )
    contestant_b = TournamentMatchToContestant(
        id=TournamentMatchToContestantID(generate_uuid()),
        tournament_match_id=MATCH_ID,
        team_id=None,
        participant_id=participant_b_id,
        score=None,
        created_at=NOW,
    )

    # Configure mocks.
    mock_brand_service.get_brand.return_value = BRAND
    mock_party_service.get_party.return_value = PARTY
    mock_email_config_service.get_config.return_value = EMAIL_CONFIG
    mock_repo.get_tournament.return_value = tournament
    mock_repo.get_match.return_value = match
    mock_repo.get_contestants_for_match.return_value = [
        contestant_a,
        contestant_b,
    ]
    mock_repo.get_participant.side_effect = lambda pid: (
        participant_a if pid == participant_a_id else participant_b
    )

    mock_participant_service.get_seats_for_users.return_value = {
        user_a_id: 'A1',
        user_b_id: 'B2',
    }

    mock_user_service.find_email_address.side_effect = lambda uid: (
        'alice@example.com' if uid == user_a_id else 'bob@example.com'
    )
    mock_user_service.find_locale.return_value = None
    mock_user_service.get_user.side_effect = lambda uid, **kw: (
        user_a if uid == user_a_id else user_b
    )

    mock_snippet_service.get_snippet_body.side_effect = _snippet_side_effect
    mock_footer_service.get_footer.return_value = Ok('-- Test Footer')

    # Patch get_default_locale inside the module.
    with patch(f'{MODULE}.get_default_locale', return_value=Locale('en')):
        tournament_notification_service.send_match_ready_emails(
            TOURNAMENT_ID, MATCH_ID
        )

    assert mock_email_service.enqueue_message.call_count == 2


@patch(f'{MODULE}.email_service')
@patch(f'{MODULE}.email_footer_service')
@patch(f'{MODULE}.snippet_service')
@patch(f'{MODULE}.user_service')
@patch(f'{MODULE}.tournament_participant_service')
@patch(f'{MODULE}.tournament_repository')
@patch(f'{MODULE}.email_config_service')
@patch(f'{MODULE}.party_service')
@patch(f'{MODULE}.brand_service')
def test_send_match_ready_emails_team_tournament(
    mock_brand_service,
    mock_party_service,
    mock_email_config_service,
    mock_repo,
    mock_participant_service,
    mock_user_service,
    mock_snippet_service,
    mock_footer_service,
    mock_email_service,
):
    """In a team tournament, all team members get emails."""
    from byceps.services.lan_tournament import tournament_notification_service

    tournament = _make_tournament(contestant_type=ContestantType.TEAM)
    match = _make_match()

    team_a_id = TournamentTeamID(generate_uuid())
    team_b_id = TournamentTeamID(generate_uuid())

    captain_a_id = UserID(generate_uuid())
    captain_b_id = UserID(generate_uuid())
    member_a2_id = UserID(generate_uuid())

    captain_a = _make_user(captain_a_id, 'CaptainA')
    captain_b = _make_user(captain_b_id, 'CaptainB')
    member_a2 = _make_user(member_a2_id, 'MemberA2')

    team_a = TournamentTeam(
        id=team_a_id,
        tournament_id=TOURNAMENT_ID,
        name='Team Alpha',
        tag='ALPH',
        description=None,
        image_url=None,
        captain_user_id=captain_a_id,
        join_code=None,
        created_at=NOW,
    )
    team_b = TournamentTeam(
        id=team_b_id,
        tournament_id=TOURNAMENT_ID,
        name='Team Beta',
        tag='BETA',
        description=None,
        image_url=None,
        captain_user_id=captain_b_id,
        join_code=None,
        created_at=NOW,
    )

    participant_cap_a = TournamentParticipant(
        id=TournamentParticipantID(generate_uuid()),
        user_id=captain_a_id,
        tournament_id=TOURNAMENT_ID,
        substitute_player=False,
        team_id=team_a_id,
        created_at=NOW,
    )
    participant_mem_a2 = TournamentParticipant(
        id=TournamentParticipantID(generate_uuid()),
        user_id=member_a2_id,
        tournament_id=TOURNAMENT_ID,
        substitute_player=False,
        team_id=team_a_id,
        created_at=NOW,
    )
    participant_cap_b = TournamentParticipant(
        id=TournamentParticipantID(generate_uuid()),
        user_id=captain_b_id,
        tournament_id=TOURNAMENT_ID,
        substitute_player=False,
        team_id=team_b_id,
        created_at=NOW,
    )

    contestant_a = TournamentMatchToContestant(
        id=TournamentMatchToContestantID(generate_uuid()),
        tournament_match_id=MATCH_ID,
        team_id=team_a_id,
        participant_id=None,
        score=None,
        created_at=NOW,
    )
    contestant_b = TournamentMatchToContestant(
        id=TournamentMatchToContestantID(generate_uuid()),
        tournament_match_id=MATCH_ID,
        team_id=team_b_id,
        participant_id=None,
        score=None,
        created_at=NOW,
    )

    # Configure mocks.
    mock_brand_service.get_brand.return_value = BRAND
    mock_party_service.get_party.return_value = PARTY
    mock_email_config_service.get_config.return_value = EMAIL_CONFIG
    mock_repo.get_tournament.return_value = tournament
    mock_repo.get_match.return_value = match
    mock_repo.get_contestants_for_match.return_value = [
        contestant_a,
        contestant_b,
    ]
    mock_repo.get_participants_for_team.side_effect = lambda tid: (
        [participant_cap_a, participant_mem_a2]
        if tid == team_a_id
        else [participant_cap_b]
    )
    mock_repo.get_team.side_effect = lambda tid: (
        team_a if tid == team_a_id else team_b
    )

    mock_participant_service.get_seats_for_users.return_value = {
        captain_a_id: 'A1',
        member_a2_id: 'A2',
        captain_b_id: 'B1',
    }

    mock_user_service.find_email_address.side_effect = lambda uid: {
        captain_a_id: 'captaina@example.com',
        member_a2_id: 'membera2@example.com',
        captain_b_id: 'captainb@example.com',
    }.get(uid)
    mock_user_service.find_locale.return_value = None
    mock_user_service.get_user.side_effect = lambda uid, **kw: {
        captain_a_id: captain_a,
        captain_b_id: captain_b,
        member_a2_id: member_a2,
    }[uid]

    mock_snippet_service.get_snippet_body.side_effect = _snippet_side_effect
    mock_footer_service.get_footer.return_value = Ok('-- Test Footer')

    with patch(f'{MODULE}.get_default_locale', return_value=Locale('en')):
        tournament_notification_service.send_match_ready_emails(
            TOURNAMENT_ID, MATCH_ID
        )

    # Team A has 2 members, Team B has 1 member → 3 emails total.
    assert mock_email_service.enqueue_message.call_count == 3


@patch(f'{MODULE}.email_service')
@patch(f'{MODULE}.email_footer_service')
@patch(f'{MODULE}.snippet_service')
@patch(f'{MODULE}.user_service')
@patch(f'{MODULE}.tournament_participant_service')
@patch(f'{MODULE}.tournament_repository')
@patch(f'{MODULE}.email_config_service')
@patch(f'{MODULE}.party_service')
@patch(f'{MODULE}.brand_service')
def test_send_match_ready_emails_skips_missing_email(
    mock_brand_service,
    mock_party_service,
    mock_email_config_service,
    mock_repo,
    mock_participant_service,
    mock_user_service,
    mock_snippet_service,
    mock_footer_service,
    mock_email_service,
):
    """Users without an email address are skipped gracefully."""
    from byceps.services.lan_tournament import tournament_notification_service

    tournament = _make_tournament(contestant_type=ContestantType.SOLO)
    match = _make_match()

    user_a_id = UserID(generate_uuid())
    user_b_id = UserID(generate_uuid())
    participant_a_id = TournamentParticipantID(generate_uuid())
    participant_b_id = TournamentParticipantID(generate_uuid())

    user_a = _make_user(user_a_id, 'Alice')
    user_b = _make_user(user_b_id, 'Bob')

    participant_a = TournamentParticipant(
        id=participant_a_id,
        user_id=user_a_id,
        tournament_id=TOURNAMENT_ID,
        substitute_player=False,
        team_id=None,
        created_at=NOW,
    )
    participant_b = TournamentParticipant(
        id=participant_b_id,
        user_id=user_b_id,
        tournament_id=TOURNAMENT_ID,
        substitute_player=False,
        team_id=None,
        created_at=NOW,
    )

    contestant_a = TournamentMatchToContestant(
        id=TournamentMatchToContestantID(generate_uuid()),
        tournament_match_id=MATCH_ID,
        team_id=None,
        participant_id=participant_a_id,
        score=None,
        created_at=NOW,
    )
    contestant_b = TournamentMatchToContestant(
        id=TournamentMatchToContestantID(generate_uuid()),
        tournament_match_id=MATCH_ID,
        team_id=None,
        participant_id=participant_b_id,
        score=None,
        created_at=NOW,
    )

    mock_brand_service.get_brand.return_value = BRAND
    mock_party_service.get_party.return_value = PARTY
    mock_email_config_service.get_config.return_value = EMAIL_CONFIG
    mock_repo.get_tournament.return_value = tournament
    mock_repo.get_match.return_value = match
    mock_repo.get_contestants_for_match.return_value = [
        contestant_a,
        contestant_b,
    ]
    mock_repo.get_participant.side_effect = lambda pid: (
        participant_a if pid == participant_a_id else participant_b
    )

    mock_participant_service.get_seats_for_users.return_value = {}

    # Alice has an email, Bob does not.
    mock_user_service.find_email_address.side_effect = lambda uid: (
        'alice@example.com' if uid == user_a_id else None
    )
    mock_user_service.find_locale.return_value = None
    mock_user_service.get_user.side_effect = lambda uid, **kw: (
        user_a if uid == user_a_id else user_b
    )

    mock_snippet_service.get_snippet_body.side_effect = _snippet_side_effect
    mock_footer_service.get_footer.return_value = Ok('-- Footer')

    with patch(f'{MODULE}.get_default_locale', return_value=Locale('en')):
        tournament_notification_service.send_match_ready_emails(
            TOURNAMENT_ID, MATCH_ID
        )

    # Only Alice gets the email; Bob is skipped.
    assert mock_email_service.enqueue_message.call_count == 1

    enqueued: Message = mock_email_service.enqueue_message.call_args[0][0]
    assert enqueued.recipients == ['alice@example.com']


@patch(f'{MODULE}.email_service')
@patch(f'{MODULE}.email_footer_service')
@patch(f'{MODULE}.snippet_service')
@patch(f'{MODULE}.user_service')
@patch(f'{MODULE}.tournament_participant_service')
@patch(f'{MODULE}.tournament_repository')
@patch(f'{MODULE}.email_config_service')
@patch(f'{MODULE}.party_service')
@patch(f'{MODULE}.brand_service')
def test_send_match_ready_emails_both_contestants_receive(
    mock_brand_service,
    mock_party_service,
    mock_email_config_service,
    mock_repo,
    mock_participant_service,
    mock_user_service,
    mock_snippet_service,
    mock_footer_service,
    mock_email_service,
):
    """Both contestants receive emails with correct opponent info."""
    from byceps.services.lan_tournament import tournament_notification_service

    tournament = _make_tournament(contestant_type=ContestantType.SOLO)
    match = _make_match()

    user_a_id = UserID(generate_uuid())
    user_b_id = UserID(generate_uuid())
    participant_a_id = TournamentParticipantID(generate_uuid())
    participant_b_id = TournamentParticipantID(generate_uuid())

    user_a = _make_user(user_a_id, 'Alice')
    user_b = _make_user(user_b_id, 'Bob')

    participant_a = TournamentParticipant(
        id=participant_a_id,
        user_id=user_a_id,
        tournament_id=TOURNAMENT_ID,
        substitute_player=False,
        team_id=None,
        created_at=NOW,
    )
    participant_b = TournamentParticipant(
        id=participant_b_id,
        user_id=user_b_id,
        tournament_id=TOURNAMENT_ID,
        substitute_player=False,
        team_id=None,
        created_at=NOW,
    )

    contestant_a = TournamentMatchToContestant(
        id=TournamentMatchToContestantID(generate_uuid()),
        tournament_match_id=MATCH_ID,
        team_id=None,
        participant_id=participant_a_id,
        score=None,
        created_at=NOW,
    )
    contestant_b = TournamentMatchToContestant(
        id=TournamentMatchToContestantID(generate_uuid()),
        tournament_match_id=MATCH_ID,
        team_id=None,
        participant_id=participant_b_id,
        score=None,
        created_at=NOW,
    )

    mock_brand_service.get_brand.return_value = BRAND
    mock_party_service.get_party.return_value = PARTY
    mock_email_config_service.get_config.return_value = EMAIL_CONFIG
    mock_repo.get_tournament.return_value = tournament
    mock_repo.get_match.return_value = match
    mock_repo.get_contestants_for_match.return_value = [
        contestant_a,
        contestant_b,
    ]
    mock_repo.get_participant.side_effect = lambda pid: (
        participant_a if pid == participant_a_id else participant_b
    )

    mock_participant_service.get_seats_for_users.return_value = {
        user_a_id: 'A1',
        user_b_id: 'B2',
    }

    mock_user_service.find_email_address.side_effect = lambda uid: (
        'alice@example.com' if uid == user_a_id else 'bob@example.com'
    )
    mock_user_service.find_locale.return_value = None
    mock_user_service.get_user.side_effect = lambda uid, **kw: (
        user_a if uid == user_a_id else user_b
    )

    mock_snippet_service.get_snippet_body.side_effect = _snippet_side_effect
    mock_footer_service.get_footer.return_value = Ok('')

    with patch(f'{MODULE}.get_default_locale', return_value=Locale('en')):
        tournament_notification_service.send_match_ready_emails(
            TOURNAMENT_ID, MATCH_ID
        )

    assert mock_email_service.enqueue_message.call_count == 2

    # Collect all enqueued messages.
    messages: list[Message] = [
        call[0][0]
        for call in mock_email_service.enqueue_message.call_args_list
    ]
    recipients = {m.recipients[0] for m in messages}
    assert recipients == {'alice@example.com', 'bob@example.com'}

    # Verify opponent names appear in correct bodies.
    alice_msg = next(m for m in messages if m.recipients[0] == 'alice@example.com')
    bob_msg = next(m for m in messages if m.recipients[0] == 'bob@example.com')

    assert 'Bob' in alice_msg.body
    assert 'B2' in alice_msg.body
    assert 'Alice' in bob_msg.body
    assert 'A1' in bob_msg.body
