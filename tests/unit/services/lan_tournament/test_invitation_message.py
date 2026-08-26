"""Typed, per-recipient match invitation building contracts."""

from dataclasses import replace
from unittest.mock import MagicMock

from babel import Locale
import pytest

from byceps.services.email.models import Message
from byceps.services.lan_tournament import tournament_notification_service as notif
from byceps.services.lan_tournament.models.contestant_type import ContestantType
from byceps.services.lan_tournament.models.tournament_match_to_contestant import (
    TournamentMatchToContestant, TournamentMatchToContestantID,
)
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipant, TournamentParticipantID,
)
from byceps.services.lan_tournament.models.tournament_team import (
    TournamentTeam, TournamentTeamID,
)
from byceps.services.user.models import UserID
from byceps.util.result import Err, Ok

from tests.helpers import generate_uuid
from tests.unit.services.lan_tournament.test_notification_service import (
    BRAND, EMAIL_CONFIG, MATCH_ID, NOW, PARTY, SENDER, TOURNAMENT_ID,
    _make_match, _make_tournament, _make_user,
)


@pytest.fixture
def invitation(monkeypatch):
    """Repository/core read boundaries; real immutable tournament DTOs."""
    user_ids = [UserID(generate_uuid()) for _ in range(3)]
    team_ids = [TournamentTeamID(generate_uuid()) for _ in range(2)]
    participants = [
        TournamentParticipant(
            id=TournamentParticipantID(generate_uuid()),
            user_id=user_id, tournament_id=TOURNAMENT_ID,
            substitute_player=False, team_id=team_ids[0 if index < 2 else 1],
            created_at=NOW,
        )
        for index, user_id in enumerate(user_ids)
    ]
    teams = [
        TournamentTeam(
            id=team_id, tournament_id=TOURNAMENT_ID, name=name, tag=None,
            description=None, image_url=None, captain_user_id=captain_id,
            join_code=None, created_at=NOW,
        )
        for team_id, name, captain_id in zip(
            team_ids, ['Alpha', 'Beta'], [user_ids[0], user_ids[2]], strict=True
        )
    ]
    contestants = [
        TournamentMatchToContestant(
            id=TournamentMatchToContestantID(generate_uuid()),
            tournament_match_id=MATCH_ID, team_id=team_id,
            participant_id=None, score=None, created_at=NOW,
        )
        for team_id in team_ids
    ]
    mocks = {}

    def mock(owner, name, **kwargs):
        value = MagicMock(**kwargs)
        monkeypatch.setattr(owner, name, value)
        mocks[name] = value
        return value

    repo = notif.tournament_repository
    mock(repo, 'get_tournament', return_value=_make_tournament(
        contestant_type=ContestantType.TEAM
    ))
    mock(repo, 'get_match', return_value=_make_match())
    mock(repo, 'get_contestants_for_match', return_value=contestants)
    mock(repo, 'get_participants_for_team', side_effect=lambda team_id: [
        participant for participant in participants if participant.team_id == team_id
    ])
    mock(repo, 'get_participant', side_effect=lambda participant_id: next(
        participant for participant in participants if participant.id == participant_id
    ))
    mock(repo, 'get_team', side_effect=lambda team_id: next(
        team for team in teams if team.id == team_id
    ))
    mock(notif.party_service, 'get_party', return_value=PARTY)
    mock(notif.brand_service, 'get_brand', return_value=BRAND)
    mock(notif.email_config_service, 'get_config', return_value=EMAIL_CONFIG)
    mock(notif.tournament_participant_service, 'get_seats_for_users',
         return_value=dict(zip(user_ids, ['A1', 'A2', 'B1'], strict=True)))
    mock(notif.user_service, 'get_user', side_effect=lambda user_id: _make_user(
        user_id, 'CaptainB' if user_id == user_ids[2] else 'CaptainA'
    ))
    mock(notif.user_service, 'find_email_address',
         side_effect=lambda user_id: f'{user_id}@example.com')
    mock(notif.user_service, 'find_locale', return_value=Locale('de'))
    mock(notif, 'get_default_locale', return_value=Locale('en'))
    mock(notif.snippet_service, 'get_snippet_body', side_effect=lambda scope, name, lang: Ok(
        f'{lang} {{tournament_name}} round {{match_round}}: '
        '{opponent_name} at {opponent_seat}. Your seat: {your_seat}\n{footer}'
        if name == notif.SNIPPET_NAME_BODY else f'{lang} {{tournament_name}} ready'
    ))
    mock(notif.email_footer_service, 'get_footer',
         side_effect=lambda brand, lang: Ok(f'{lang} footer'))
    mock(notif.email_service, 'enqueue_message')
    mock(notif.email_service, 'send_email')
    return user_ids, participants, contestants, mocks


# fmt: off
@pytest.mark.parametrize('missing_de', [None, 'body', 'subject', 'footer', 'all'])
# fmt: on
def test_message_preserves_team_audience_and_locale_fallback(invitation, missing_de):
    user_ids, _, _, mocks = invitation
    get_snippet = mocks['get_snippet_body'].side_effect

    def snippet(scope, name, lang):
        if lang == 'de' and (
            missing_de == 'all'
            or missing_de == 'body' and name == notif.SNIPPET_NAME_BODY
            or missing_de == 'subject' and name == notif.SNIPPET_NAME_SUBJECT
        ):
            return Err('missing')
        return get_snippet(scope, name, lang)

    mocks['get_snippet_body'].side_effect = snippet
    if missing_de == 'footer':
        mocks['get_footer'].side_effect = lambda brand, lang: (
            Err('missing') if lang == 'de' else Ok('en footer')
        )
    lang = 'de' if missing_de is None else 'en'
    for index, user_id in enumerate(user_ids):
        result = notif.build_match_invitation_message(TOURNAMENT_ID, MATCH_ID, user_id)
        assert result.is_ok()
        message = result.unwrap()
        assert isinstance(message, Message)
        assert message.sender == SENDER
        assert message.recipients == [f'{user_id}@example.com']
        assert message.subject == f'{lang} CS2 Cup ready'
        opponent = 'Beta (captain: CaptainB)' if index < 2 else 'Alpha (captain: CaptainA)'
        opponent_seat = 'B1' if index < 2 else 'A1'
        your_seat = ['A1', 'A2', 'B1'][index]
        assert message.body == (
            f'{lang} CS2 Cup round 1: {opponent} at {opponent_seat}. '
            f'Your seat: {your_seat}\n{lang} footer'
        )
    mocks['get_participants_for_team'].assert_any_call(
        mocks['get_contestants_for_match'].return_value[0].team_id
    )
    mocks['get_seats_for_users'].assert_called_with(set(user_ids), PARTY.id)
    mocks['enqueue_message'].assert_not_called()
    mocks['send_email'].assert_not_called()


# fmt: off
@pytest.mark.parametrize(('missing', 'error'), [
    ('address', 'email_address_missing'),
    ('config', 'email_config_missing'),
    ('sender', 'email_config_missing'),
    ('body', 'email_body_snippet_missing'),
    ('subject', 'email_subject_snippet_missing'),
    ('footer', 'email_footer_missing'),
])
# fmt: on
def test_missing_address_or_snippet_returns_err(invitation, missing, error):
    user_ids, _, _, mocks = invitation
    if missing == 'address':
        mocks['find_email_address'].side_effect = None
        mocks['find_email_address'].return_value = None
    elif missing == 'config':
        mocks['get_config'].side_effect = notif.email_config_service.UnknownEmailConfigIdError('missing')
    elif missing == 'sender':
        mocks['get_config'].return_value = replace(
            EMAIL_CONFIG, sender=replace(SENDER, address='')
        )
    elif missing == 'footer':
        mocks['get_footer'].side_effect = lambda brand, lang: Err('missing')
    else:
        original = mocks['get_snippet_body'].side_effect
        snippet_name = notif.SNIPPET_NAME_BODY if missing == 'body' else notif.SNIPPET_NAME_SUBJECT
        mocks['get_snippet_body'].side_effect = lambda scope, name, lang: (
            Err('missing') if name == snippet_name else original(scope, name, lang)
        )
    result = notif.build_match_invitation_message(TOURNAMENT_ID, MATCH_ID, user_ids[0])
    assert result.is_err()
    assert result.unwrap_err() == error
    mocks['enqueue_message'].assert_not_called()
    mocks['send_email'].assert_not_called()


# fmt: off
@pytest.mark.parametrize('template', ['{missing}', '{', '{0}', '{tournament_name.x}', '{tournament_name[a]}'])
@pytest.mark.parametrize('snippet_name', [notif.SNIPPET_NAME_BODY, notif.SNIPPET_NAME_SUBJECT])
# fmt: on
def test_malformed_invitation_template_returns_err(invitation, template, snippet_name):
    user_ids, _, _, mocks = invitation
    original = mocks['get_snippet_body'].side_effect
    mocks['get_snippet_body'].side_effect = lambda scope, name, lang: (
        Ok(template) if name == snippet_name else original(scope, name, lang)
    )
    result = notif.build_match_invitation_message(TOURNAMENT_ID, MATCH_ID, user_ids[0])
    assert result.is_err()
    assert result.unwrap_err() == 'email_template_formatting_failed'
    mocks['enqueue_message'].assert_not_called()
    mocks['send_email'].assert_not_called()


# fmt: off
@pytest.mark.parametrize('locale', [None, Locale('fr')])
# fmt: on
def test_solo_message_uses_default_locale_and_unknown_seats(invitation, locale):
    user_ids, participants, contestants, mocks = invitation
    mocks['get_contestants_for_match'].return_value = [
        replace(contestant, team_id=None, participant_id=participant.id)
        for contestant, participant in zip(contestants, [participants[0], participants[2]], strict=True)
    ]
    mocks['get_tournament'].return_value = _make_tournament()
    mocks['get_match'].return_value = replace(_make_match(), round=None)
    mocks['get_seats_for_users'].return_value = {}
    mocks['find_locale'].return_value = locale
    original = mocks['get_snippet_body'].side_effect
    mocks['get_snippet_body'].side_effect = lambda scope, name, lang: (
        Err('missing') if lang == 'fr' else original(scope, name, lang)
    )
    result = notif.build_match_invitation_message(TOURNAMENT_ID, MATCH_ID, user_ids[0])
    assert result.is_ok()
    assert result.unwrap().body == 'en CS2 Cup round ?: CaptainB at ?. Your seat: ?\nen footer'
    mocks['get_participants_for_team'].assert_not_called()


# fmt: off
@pytest.mark.parametrize(('invalid', 'error'), [
    ('outsider', 'recipient_not_on_unique_match_side'),
    ('removed_member', 'recipient_not_on_unique_match_side'),
    ('both_sides', 'recipient_not_on_unique_match_side'),
    ('wrong_tournament', 'match_tournament_mismatch'),
    ('one_side', 'match_requires_two_contestants'),
    ('three_sides', 'match_requires_two_contestants'),
])
# fmt: on
def test_invitation_rejects_invalid_audience_or_match(invitation, invalid, error):
    user_ids, participants, contestants, mocks = invitation
    recipient_id = user_ids[1]
    if invalid == 'outsider':
        recipient_id = UserID(generate_uuid())
    elif invalid == 'removed_member':
        mocks['get_participants_for_team'].side_effect = lambda team_id: [
            participant for participant in participants
            if participant.team_id == team_id and participant.user_id != recipient_id
        ]
    elif invalid == 'both_sides':
        mocks['get_participants_for_team'].side_effect = None
        mocks['get_participants_for_team'].return_value = participants
    elif invalid == 'wrong_tournament':
        mocks['get_match'].return_value = replace(
            _make_match(), tournament_id=generate_uuid()
        )
    else:
        mocks['get_contestants_for_match'].return_value = (
            contestants[:1] if invalid == 'one_side' else contestants + contestants[:1]
        )
    result = notif.build_match_invitation_message(TOURNAMENT_ID, MATCH_ID, recipient_id)
    assert result.is_err()
    assert result.unwrap_err() == error
    mocks['find_email_address'].assert_not_called()
    mocks['enqueue_message'].assert_not_called()
    mocks['send_email'].assert_not_called()


def test_returned_message_has_no_delivery_or_setup_side_effects(invitation, monkeypatch):
    user_ids, _, _, mocks = invitation
    create_snippet = MagicMock()
    monkeypatch.setattr(notif.snippet_service, 'create_snippet', create_snippet)
    result = notif.build_match_invitation_message(TOURNAMENT_ID, MATCH_ID, user_ids[1])
    assert result.is_ok()
    message = result.unwrap()
    assert isinstance(message, Message)
    assert message.recipients == [f'{user_ids[1]}@example.com']
    mocks['enqueue_message'].assert_not_called()
    mocks['send_email'].assert_not_called()
    create_snippet.assert_not_called()

    # The old sender deliberately still enqueues all active recipients;
    # its content is built through the same rendering tail, not a new one.
    assert notif.send_match_ready_emails(TOURNAMENT_ID, MATCH_ID) is None
    assert mocks['enqueue_message'].call_count == 3
    legacy_message = next(
        call.args[0] for call in mocks['enqueue_message'].call_args_list
        if call.args[0].recipients == message.recipients
    )
    assert legacy_message == message
    mocks['send_email'].assert_not_called()
    create_snippet.assert_not_called()
