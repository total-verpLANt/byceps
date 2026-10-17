"""
tests.unit.services.lan_tournament.test_match_mail_senders
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Unit tests for the opponent-ready and FFA lobby mail senders and their
default snippets.
"""

from dataclasses import replace
from string import Formatter
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from babel import Locale
import pytest

from byceps.services.email import email_config_service
from byceps.services.email.models import Message
from byceps.services.lan_tournament import (
    tournament_notification_service as notif,
)
from byceps.services.lan_tournament.models.match_readiness import (
    ContestantIdentity,
)
from byceps.services.lan_tournament.models.tournament import TournamentID
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
from byceps.services.user.models import UserID
from byceps.util.result import Ok

from tests.helpers import generate_uuid
from tests.unit.services.lan_tournament.test_notification_service import (
    _make_match,
    _make_tournament,
    _make_user,
    BRAND,
    EMAIL_CONFIG,
    MATCH_ID,
    NOW,
    PARTY,
    TOURNAMENT_ID,
)


MODULE = 'byceps.services.lan_tournament.tournament_notification_service'


class _World:
    """The rows the senders read, kept in memory."""

    def __init__(self) -> None:
        self.user_ids: dict[str, UserID] = {}
        self.screen_names: dict[UserID, str] = {}
        self.seats: dict[UserID, str] = {}
        self.failing_user_ids: set[UserID] = set()
        self.participants: dict[
            TournamentParticipantID, TournamentParticipant
        ] = {}
        self.members: dict[TournamentTeamID, list[TournamentParticipant]] = {}
        self.teams: dict[TournamentTeamID, TournamentTeam] = {}
        self.contestants: list[TournamentMatchToContestant] = []
        self.enqueue = MagicMock(name='enqueue_message')
        self.log = MagicMock(name='log')
        self.config_service = MagicMock(name='email_config_service')

    def user(self, name: str, seat: str | None = None) -> UserID:
        """Return the user of that screen name, creating it on first use."""
        user_id = self.user_ids.get(name)
        if user_id is None:
            user_id = UserID(generate_uuid())
            self.user_ids[name] = user_id
            self.screen_names[user_id] = name
        if seat is not None:
            self.seats[user_id] = seat
        return user_id

    def add_solo(
        self, name: str, seat: str | None = None
    ) -> ContestantIdentity:
        participant = self._add_participant(self.user(name, seat), None)
        self._add_contestant(participant_id=participant.id, team_id=None)
        return ContestantIdentity(kind='participant', id=participant.id)

    def add_team(
        self, team_name: str, members: dict[str, str | None]
    ) -> ContestantIdentity:
        """Add a team contestant; the first member is the captain."""
        team_id = TournamentTeamID(generate_uuid())
        user_ids = [self.user(name, seat) for name, seat in members.items()]
        self.teams[team_id] = TournamentTeam(
            id=team_id,
            tournament_id=TOURNAMENT_ID,
            name=team_name,
            tag=None,
            description=None,
            image_url=None,
            captain_user_id=user_ids[0],
            join_code=None,
            created_at=NOW,
        )
        self.members[team_id] = [
            self._add_participant(user_id, team_id) for user_id in user_ids
        ]
        self._add_contestant(participant_id=None, team_id=team_id)
        return ContestantIdentity(kind='team', id=team_id)

    def messages(self) -> list[Message]:
        return [call.args[0] for call in self.enqueue.call_args_list]

    def recipients(self) -> list[str]:
        return [message.recipients[0] for message in self.messages()]

    def _add_participant(
        self, user_id: UserID, team_id: TournamentTeamID | None
    ) -> TournamentParticipant:
        participant = TournamentParticipant(
            id=TournamentParticipantID(generate_uuid()),
            user_id=user_id,
            tournament_id=TOURNAMENT_ID,
            substitute_player=False,
            team_id=team_id,
            created_at=NOW,
        )
        self.participants[participant.id] = participant
        return participant

    def _add_contestant(
        self,
        *,
        participant_id: TournamentParticipantID | None,
        team_id: TournamentTeamID | None,
    ) -> None:
        self.contestants.append(
            TournamentMatchToContestant(
                id=TournamentMatchToContestantID(generate_uuid()),
                tournament_match_id=MATCH_ID,
                team_id=team_id,
                participant_id=participant_id,
                score=None,
                created_at=NOW,
            )
        )


@pytest.fixture(scope='module')
def default_contents():
    """The default snippet texts, as `create_match_ready_email_snippets`
    hands them to the snippet setup.
    """
    with patch(f'{MODULE}._create_missing_snippets') as create:
        create.return_value = False
        notif.create_match_ready_email_snippets(BRAND, MagicMock())
    return create.call_args.args[2]


@pytest.fixture
def world(monkeypatch, default_contents):
    """Collaborator boundaries of the senders; the snippets are the real
    defaults, so a key the sender does not pass fails the render.
    """
    world = _World()

    repo = MagicMock(name='tournament_repository')
    repo.get_tournament.return_value = _make_tournament()
    repo.get_match.return_value = _make_match()
    repo.get_contestants_for_match.side_effect = lambda match_id: list(
        world.contestants
    )
    repo.get_participants_for_team.side_effect = lambda team_id: world.members[
        team_id
    ]
    repo.get_participant.side_effect = lambda participant_id: (
        world.participants[participant_id]
    )
    repo.get_team.side_effect = lambda team_id: world.teams[team_id]

    party_service = MagicMock(name='party_service')
    party_service.get_party.return_value = PARTY

    brand_service = MagicMock(name='brand_service')
    brand_service.get_brand.return_value = BRAND

    world.config_service.UnknownEmailConfigIdError = (
        email_config_service.UnknownEmailConfigIdError
    )
    world.config_service.get_config.return_value = EMAIL_CONFIG

    participant_service = MagicMock(name='tournament_participant_service')
    participant_service.get_seats_for_users.side_effect = (
        lambda user_ids, party_id: {
            user_id: world.seats[user_id]
            for user_id in user_ids
            if user_id in world.seats
        }
    )

    def find_email_address(user_id: UserID) -> str:
        if user_id in world.failing_user_ids:
            raise RuntimeError('user lookup failed')
        return f'{world.screen_names[user_id].lower()}@example.com'

    user_service = MagicMock(name='user_service')
    user_service.get_user.side_effect = lambda user_id: _make_user(
        user_id, world.screen_names[user_id]
    )
    user_service.find_email_address.side_effect = find_email_address
    user_service.find_locale.return_value = Locale('en')

    snippet_service = MagicMock(name='snippet_service')
    snippet_service.get_snippet_body.side_effect = (
        lambda scope, name, language_code: Ok(
            default_contents[(name, language_code)]
        )
    )

    footer_service = MagicMock(name='email_footer_service')
    footer_service.get_footer.return_value = Ok('FOOTER')

    email_service = MagicMock(name='email_service')
    email_service.enqueue_message = world.enqueue

    for name, replacement in {
        'tournament_repository': repo,
        'party_service': party_service,
        'brand_service': brand_service,
        'email_config_service': world.config_service,
        'tournament_participant_service': participant_service,
        'user_service': user_service,
        'snippet_service': snippet_service,
        'email_footer_service': footer_service,
        'email_service': email_service,
        'get_default_locale': lambda: Locale('en'),
        'log': world.log,
    }.items():
        monkeypatch.setattr(notif, name, replacement)

    world.repo = repo
    return world


def _send_opponent_ready(ready: ContestantIdentity) -> None:
    notif.send_opponent_ready_emails(TOURNAMENT_ID, MATCH_ID, ready)


def _send_ffa_lobby(ready: ContestantIdentity | None) -> None:
    notif.send_ffa_lobby_emails(TOURNAMENT_ID, MATCH_ID)


both_senders = pytest.mark.parametrize(
    'send',
    [_send_opponent_ready, _send_ffa_lobby],
    ids=['opponent-ready', 'ffa-lobby'],
)


# --------------------------------------------------------------------- #
# opponent ready
# --------------------------------------------------------------------- #


def test_opponent_ready_mails_only_the_other_solo_side(world):
    alice = world.add_solo('Alice', 'A1')
    world.add_solo('Bob', 'B2')

    _send_opponent_ready(alice)

    assert world.recipients() == ['bob@example.com']
    message = world.messages()[0]
    assert message.subject == '[CS2 Cup] Your opponent is ready (round 1)'
    assert 'Opponent: Alice' in message.body
    assert "Opponent's seat: A1" in message.body
    assert 'Your seat: B2' in message.body
    assert 'Bob' not in message.body
    assert message.body.endswith('FOOTER')


def test_opponent_ready_mails_every_member_of_the_other_team(world):
    ready = world.add_team('Alpha', {'Ann': 'A1', 'Al': 'A2'})
    world.add_team('Beta', {'Bea': 'B1', 'Ben': 'B2', 'Bo': None})

    _send_opponent_ready(ready)

    assert sorted(world.recipients()) == [
        'bea@example.com',
        'ben@example.com',
        'bo@example.com',
    ]
    body_by_recipient = {
        message.recipients[0]: message.body for message in world.messages()
    }
    for body in body_by_recipient.values():
        assert 'Opponent: Alpha (captain: Ann)' in body
        assert "Opponent's seat: A1" in body
    assert 'Your seat: B1' in body_by_recipient['bea@example.com']
    assert 'Your seat: B2' in body_by_recipient['ben@example.com']
    assert 'Your seat: ?' in body_by_recipient['bo@example.com']


def test_opponent_ready_skips_user_on_both_sides(world):
    ready = world.add_team('Alpha', {'Ann': 'A1', 'Shared': 'S1'})
    world.add_team('Beta', {'Shared': None, 'Ben': 'B1'})

    _send_opponent_ready(ready)

    assert world.recipients() == ['ben@example.com']


# fmt: off
@pytest.mark.parametrize(
    'kind, use_participant_id',
    [
        pytest.param('participant', False, id='unknown-id'),
        pytest.param('team', True, id='id-of-the-wrong-kind'),
        pytest.param('referee', True, id='unknown-kind'),
    ],
)
# fmt: on
def test_opponent_ready_unknown_ready_contestant_sends_nothing(
    world, kind, use_participant_id
):
    alice = world.add_solo('Alice', 'A1')
    world.add_solo('Bob', 'B2')
    ready = ContestantIdentity(
        kind=kind,
        id=alice.id if use_participant_id else generate_uuid(),
    )

    _send_opponent_ready(ready)

    world.enqueue.assert_not_called()
    world.log.exception.assert_not_called()


# --------------------------------------------------------------------- #
# FFA lobby
# --------------------------------------------------------------------- #


def test_ffa_lobby_mails_each_member_once_with_other_contestants(world):
    world.add_solo('Alice', 'A1')
    world.add_solo('Bob', 'B2')
    world.add_solo('Cara', 'C3')

    _send_ffa_lobby(ready=None)

    assert sorted(world.recipients()) == [
        'alice@example.com',
        'bob@example.com',
        'cara@example.com',
    ]
    body_by_recipient = {
        message.recipients[0]: message.body for message in world.messages()
    }
    alice_body = body_by_recipient['alice@example.com']
    assert '- Bob (B2)\n- Cara (C3)\n' in alice_body
    assert 'Alice' not in alice_body
    assert 'Your seat: A1' in alice_body
    assert 'Players in the lobby: 3' in alice_body
    bob_body = body_by_recipient['bob@example.com']
    assert '- Alice (A1)\n- Cara (C3)\n' in bob_body
    assert 'Bob' not in bob_body
    cara_body = body_by_recipient['cara@example.com']
    assert '- Alice (A1)\n- Bob (B2)\n' in cara_body
    assert 'Cara' not in cara_body
    assert world.messages()[0].subject == (
        '[CS2 Cup] Your lobby is ready (round 1)'
    )


def test_ffa_lobby_mails_each_team_member_once_without_own_team(world):
    world.add_team('Alpha', {'Ann': 'A1', 'Al': 'A2'})
    world.add_solo('Bob', 'B2')

    _send_ffa_lobby(ready=None)

    assert sorted(world.recipients()) == [
        'al@example.com',
        'ann@example.com',
        'bob@example.com',
    ]
    body_by_recipient = {
        message.recipients[0]: message.body for message in world.messages()
    }
    for recipient in ('ann@example.com', 'al@example.com'):
        assert '- Bob (B2)\n' in body_by_recipient[recipient]
        assert 'Alpha' not in body_by_recipient[recipient]
    assert (
        '- Alpha (captain: Ann) (A1)\n' in body_by_recipient['bob@example.com']
    )


def test_ffa_lobby_mails_a_user_on_two_contestants_once(world):
    world.add_team('Beta', {'Shared': 'S1', 'Ben': 'B1'})
    world.add_solo('Cara', 'C3')
    world.add_solo('Shared')

    _send_ffa_lobby(ready=None)

    assert sorted(world.recipients()) == [
        'ben@example.com',
        'cara@example.com',
        'shared@example.com',
    ]
    body_by_recipient = {
        message.recipients[0]: message.body for message in world.messages()
    }
    shared_body = body_by_recipient['shared@example.com']
    assert '- Cara (C3)\n' in shared_body
    assert 'Beta' not in shared_body


def test_ffa_lobby_renders_braces_in_names_literally(world):
    world.add_solo('Alice', 'A1')
    world.add_solo('{evil}', 'B2')

    _send_ffa_lobby(ready=None)

    assert len(world.messages()) == 2
    alice_body = world.messages()[0].body
    assert world.messages()[0].recipients == ['alice@example.com']
    assert '- {evil} (B2)' in alice_body
    assert '{{' not in alice_body


def test_ffa_lobby_placeholders_in_names_are_not_expanded(world):
    world.add_solo('Alice', 'A1')
    world.add_solo('{footer}{your_seat}{0.__class__}', 'B2')

    _send_ffa_lobby(ready=None)

    alice_body = world.messages()[0].body
    assert '- {footer}{your_seat}{0.__class__} (B2)' in alice_body
    assert alice_body.count('FOOTER') == 1


def test_opponent_ready_renders_braces_in_names_literally(world):
    alice = world.add_solo('{footer}{your_seat}', 'A1')
    world.add_solo('Bob', 'B2')

    _send_opponent_ready(alice)

    message = world.messages()[0]
    assert 'Opponent: {footer}{your_seat}' in message.body
    assert '{{' not in message.body
    assert message.body.count('FOOTER') == 1
    assert 'Your seat: B2' in message.body


def test_ffa_lobby_with_one_contestant_sends_nothing(world):
    world.add_solo('Alice', 'A1')

    _send_ffa_lobby(ready=None)

    world.enqueue.assert_not_called()


# --------------------------------------------------------------------- #
# failure handling
# --------------------------------------------------------------------- #


@both_senders
def test_senders_never_raise_on_unexpected_error(world, send):
    alice = world.add_solo('Alice', 'A1')
    world.add_solo('Bob', 'B2')
    world.repo.get_contestants_for_match.side_effect = RuntimeError('db down')

    send(alice)

    world.log.exception.assert_called_once()
    world.enqueue.assert_not_called()


@both_senders
def test_a_failing_recipient_does_not_stop_the_others(world, send):
    alice = world.add_solo('Alice', 'A1')
    world.add_team('Beta', {'Bea': 'B1', 'Ben': 'B2', 'Bo': 'B3'})
    world.failing_user_ids.add(world.user('Ben'))

    send(alice)

    world.log.exception.assert_called_once()
    assert 'ben@example.com' not in world.recipients()
    assert {'bea@example.com', 'bo@example.com'} <= set(world.recipients())


@both_senders
def test_missing_email_config_sends_nothing(world, send):
    alice = world.add_solo('Alice', 'A1')
    world.add_solo('Bob', 'B2')
    world.config_service.get_config.side_effect = (
        email_config_service.UnknownEmailConfigIdError('no config')
    )

    send(alice)

    world.enqueue.assert_not_called()
    world.log.exception.assert_not_called()
    world.log.warning.assert_called_once()


@both_senders
def test_match_of_another_tournament_sends_nothing(world, send):
    alice = world.add_solo('Alice', 'A1')
    world.add_solo('Bob', 'B2')
    world.repo.get_match.return_value = replace(
        _make_match(), tournament_id=TournamentID(generate_uuid())
    )

    send(alice)

    world.enqueue.assert_not_called()
    world.log.warning.assert_called_once()


@both_senders
def test_match_without_round_renders_a_question_mark(world, send):
    alice = world.add_solo('Alice', 'A1')
    world.add_solo('Bob', 'B2')
    world.repo.get_match.return_value = replace(_make_match(), round=None)

    send(alice)

    assert world.messages()
    for message in world.messages():
        assert message.subject.endswith('(round ?)')


# --------------------------------------------------------------------- #
# default snippets
# --------------------------------------------------------------------- #


_NEW_PAIRS = [
    (name, language_code)
    for name in (
        notif.SNIPPET_NAME_OPPONENT_READY_BODY,
        notif.SNIPPET_NAME_OPPONENT_READY_SUBJECT,
        notif.SNIPPET_NAME_FFA_LOBBY_BODY,
        notif.SNIPPET_NAME_FFA_LOBBY_SUBJECT,
    )
    for language_code in ('en', 'de')
]

_ROUND_KEYS = {'tournament_name', 'match_round'}
_EXPECTED_FIELDS = {
    notif.SNIPPET_NAME_OPPONENT_READY_BODY: _ROUND_KEYS
    | {'opponent_name', 'opponent_seat', 'your_seat', 'footer'},
    notif.SNIPPET_NAME_OPPONENT_READY_SUBJECT: _ROUND_KEYS,
    notif.SNIPPET_NAME_FFA_LOBBY_BODY: _ROUND_KEYS
    | {'lobby_members', 'member_count', 'your_seat', 'footer'},
    notif.SNIPPET_NAME_FFA_LOBBY_SUBJECT: _ROUND_KEYS,
}


def test_new_snippet_pairs_are_required_and_created():
    assert set(_NEW_PAIRS) <= set(notif._REQUIRED_SNIPPET_PAIRS)
    existing = [
        SimpleNamespace(name=name, language_code=language_code)
        for name, language_code in notif._REQUIRED_SNIPPET_PAIRS
        if (name, language_code) not in _NEW_PAIRS
    ]

    with patch(f'{MODULE}.snippet_service') as snippet_service:
        snippet_service.get_snippets_for_scope_with_current_versions.return_value = existing
        created = notif.create_match_ready_email_snippets(BRAND, MagicMock())

    assert created is True
    created_by_pair = {
        (call.args[1], call.args[2]): call.args[4]
        for call in snippet_service.create_snippet.call_args_list
    }
    assert snippet_service.create_snippet.call_count == len(_NEW_PAIRS)
    assert set(created_by_pair) == set(_NEW_PAIRS)
    for (name, _), content in created_by_pair.items():
        fields = {
            field for _, field, _, _ in Formatter().parse(content) if field
        }
        assert fields == _EXPECTED_FIELDS[name], name
