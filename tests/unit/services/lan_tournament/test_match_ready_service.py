"""Immutable readiness storage and direct-service adversarial regressions."""

from dataclasses import fields, FrozenInstanceError, replace
from datetime import datetime
import inspect
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from byceps.services.lan_tournament import tournament_match_service as facade
from byceps.services.lan_tournament import (
    tournament_readiness_service as service,
    tournament_repository,
)
from byceps.services.lan_tournament.events import MatchReadyRevokedEvent
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.match_readiness import (
    ContestantIdentity,
    MatchPairing,
)
from byceps.services.lan_tournament.models.tournament_match import (
    MatchInvitationID,
    MatchSide,
    TournamentMatch,
)
from byceps.services.lan_tournament.models.tournament_match_to_contestant import (
    TournamentMatchToContestant,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.util.result import Err, Ok

NOW = datetime(2026, 10, 5)


class RepoStub:
    """Writes replace snapshots, never mutate a returned frozen DTO."""

    def __init__(self):
        self.users = [uuid4(), uuid4()]
        self.tournament = SimpleNamespace(
            id=uuid4(),
            tournament_status=TournamentStatus.ONGOING,
            game_format=GameFormat.ONE_V_ONE,
            playoff_game_format=GameFormat.FREE_FOR_ALL,
        )
        self.match = TournamentMatch(
            id=uuid4(),
            tournament_id=self.tournament.id,
            group_order=None,
            match_order=1,
            round=1,
            next_match_id=None,
            confirmed_by=None,
            created_at=NOW,
            pairing_id=uuid4(),
            pairing_generation=1,
        )
        self.participants = {
            uuid4(): SimpleNamespace(
                user_id=user,
                tournament_id=self.tournament.id,
                removed_at=None,
                team_id=None,
            )
            for user in self.users
        }
        for key, participant in self.participants.items():
            participant.id = key
        self.contestants = [
            TournamentMatchToContestant(
                id=uuid4(),
                tournament_match_id=self.match.id,
                participant_id=key,
                team_id=None,
                score=None,
                created_at=NOW,
            )
            for key in self.participants
        ]
        identities = [
            ContestantIdentity(kind='participant', id=key)
            for key in self.participants
        ]
        self.pairing = MatchPairing(
            id=self.match.pairing_id,
            match_id=self.match.id,
            tournament_id=self.tournament.id,
            generation=1,
            side_a=identities[0],
            side_b=identities[1],
        )
        self.order = []
        self.committed = 0
        self.ready_writes = 0

    def find_match(self, match_id):
        self.order.append('identity')
        return self.match

    def get_tournament_for_update(self, tournament_id):
        self.order.append('tournament')
        return self.tournament

    def get_tournament(self, tournament_id):
        return self.tournament

    def get_match_for_update(self, match_id):
        self.order.append('match')
        return self.match

    def get_contestants_for_match(self, match_id):
        self.order.append('contestants')
        return list(self.contestants)

    def get_match_pairing(self, match_id):
        self.order.append('pairing')
        return self.pairing

    def find_participant_fresh(self, participant_id):
        self.order.append('participant_fresh')
        return self.participants.get(participant_id)

    def set_side_ready_flush(self, match_id, side, ready_at, ready_by):
        self.ready_writes += 1
        self.match = replace(
            self.match,
            **{
                f'ready_at_{side.value}': ready_at,
                f'ready_by_{side.value}': ready_by,
            },
        )

    def clear_side_ready_flush(self, match_id, side):
        self.match = replace(
            self.match,
            **{f'ready_at_{side.value}': None, f'ready_by_{side.value}': None},
        )

    def set_side_invitation_hold_flush(self, match_id, side, held):
        self.match = replace(
            self.match, **{f'invitation_hold_{side.value}': held}
        )

    def set_readiness_revision_flush(self, match_id, revision):
        self.match = replace(self.match, readiness_revision=revision)


@pytest.fixture
def repo(monkeypatch):
    stub = RepoStub()
    monkeypatch.setattr(service, 'repository', stub)
    monkeypatch.setattr(facade, 'tournament_repository', stub)
    monkeypatch.setattr(
        service.authority.tournament_participant_service,
        'find_participant',
        stub.participants.get,
    )
    monkeypatch.setattr(
        service.authority, 'get_permissions_for_user', lambda user: set()
    )
    monkeypatch.setattr(
        service.authority.tournament_orga_service,
        'is_orga_for_tournament',
        lambda user, tid: False,
    )
    stub.log_mock = Mock()
    monkeypatch.setattr(service, 'create_log_entry', stub.log_mock)
    stub.pending_ids: tuple[MatchInvitationID, ...] = (
        MatchInvitationID(uuid4()), MatchInvitationID(uuid4()),
    )
    stub.dispatch_mock = Mock(return_value=Ok(None))

    def reconcile(match_ids, *, occurred_at):
        assert tuple(match_ids) == (stub.match.id,)
        assert occurred_at.tzinfo is None
        assert stub.committed == 0
        stub.dispatch_mock.assert_not_called()
        return Ok(stub.pending_ids)

    stub.reconcile_mock = Mock(side_effect=reconcile)
    monkeypatch.setattr(service, 'reconcile_invitations_flush', stub.reconcile_mock)
    monkeypatch.setattr(service, 'dispatch_pending_invitations', stub.dispatch_mock)
    return stub


def claim(repo, side=MatchSide.A, user=None, **kwargs):
    return facade.claim_ready(
        repo.match.id,
        side,
        user or repo.users[0],
        expected_pairing_generation=kwargs.get(
            'generation', repo.match.pairing_generation
        ),
        expected_readiness_revision=kwargs.get(
            'revision', repo.match.readiness_revision
        ),
    )


def revoke(repo, **kwargs):
    return facade.revoke_ready(
        repo.match.id,
        MatchSide.A,
        repo.users[0],
        expected_pairing_generation=kwargs.get(
            'generation', repo.match.pairing_generation
        ),
        expected_readiness_revision=kwargs.get(
            'revision', repo.match.readiness_revision
        ),
    )


def test_claim_returns_fresh_frozen_readiness(repo):
    before = repo.match
    result = claim(repo)
    assert result.is_ok()
    change = result.unwrap()
    assert before.ready_at_a is None and before.ready_by_a is None
    assert change.match is not before and change.match == repo.match
    assert change.readiness == facade.get_match_readiness(
        repo.match, repo.contestants
    )
    assert change.readiness.ready_sides == (MatchSide.A,)
    assert change.match.ready_by_a == repo.users[0]
    assert change.pending_invitation_ids == repo.pending_ids
    repo.dispatch_mock.assert_not_called()
    for obj, field in (
        (change, 'actor_role'),
        (change.match, 'ready_at_a'),
        (change.readiness, 'status'),
    ):
        with pytest.raises(FrozenInstanceError):
            setattr(obj, field, None)
    assert repo.committed == 0


def test_repeat_claim_preserves_actor_time_and_audit(repo):
    assert claim(repo).is_ok()
    original = repo.match
    repeated = claim(repo)
    assert repeated.unwrap_err() == 'readiness_conflict'
    assert repo.match == original
    assert repo.ready_writes == repo.log_mock.call_count == 1
    assert repo.committed == 0


def test_generation_revision_and_revoke_reclaim_aba(repo):
    assert claim(repo).is_ok()
    claimed_revision = repo.match.readiness_revision
    assert revoke(repo).is_ok()
    assert claim(repo).is_ok()
    original, audits = repo.match, repo.log_mock.call_count
    for kwargs in ({'revision': claimed_revision}, {'generation': 0}):
        assert revoke(repo, **kwargs).unwrap_err() == 'readiness_conflict'
        assert repo.match == original and repo.log_mock.call_count == audits


def test_revoke_audit_and_own_hold_only(repo):
    assert claim(repo).is_ok()
    repo.match = replace(repo.match, invitation_hold_b=True)
    result = revoke(repo)
    assert result.is_ok()
    change = result.unwrap()
    assert change.match.ready_at_a is None and change.match.ready_by_a is None
    assert change.match.invitation_hold_a and change.match.invitation_hold_b
    data = repo.log_mock.call_args.kwargs['data']
    assert data['previous_display_status'] == 'partially_ready'
    assert data['side'] == 'a' and data['actor_role'] == 'player'
    assert data['contestant_id'] == str(repo.pairing.side_a.id)
    assert data['contestant_kind'] == 'participant'
    assert data['pairing_generation'] == 1 and data['readiness_revision'] == 2
    assert datetime.fromisoformat(data['revoked_at']).tzinfo is None
    assert repo.log_mock.call_args.args[2] == repo.users[0]
    assert repo.log_mock.call_args.kwargs['commit'] is False
    assert claim(repo).is_ok()
    assert not repo.match.invitation_hold_a and repo.match.invitation_hold_b


def test_revoke_without_reason_returns_to_previous_state(repo):
    before = facade.get_match_readiness(repo.match, repo.contestants)
    assert claim(repo).is_ok()
    result = revoke(repo)
    assert result.is_ok()
    change = result.unwrap()
    assert change.readiness.status == before.status
    assert change.readiness.ready_sides == before.ready_sides == ()
    assert change.match.ready_at_a is None and change.match.ready_by_a is None
    assert change.match.readiness_revision == 2
    assert repo.log_mock.call_count == 2
    assert repo.log_mock.call_args.args[0] == 'match-ready-revoked'
    assert repo.committed == 0
    for target in (facade.revoke_ready, service.revoke_ready_flush):
        assert 'reason' not in inspect.signature(target).parameters


def test_revoke_writes_no_revocation_record(repo):
    assert claim(repo).is_ok()
    result = revoke(repo)
    assert result.is_ok()
    change = result.unwrap()
    assert not hasattr(tournament_repository, 'set_ready_revocation_flush')
    recorded = [
        f.name
        for f in fields(repo.match)
        if f.name.startswith('ready_revoked') and getattr(repo.match, f.name)
    ]
    assert recorded == []
    data = repo.log_mock.call_args.kwargs['data']
    assert 'reason' not in data and 'revoked_at' in data
    event = change.events[0]
    assert isinstance(event, MatchReadyRevokedEvent)
    assert 'reason' not in {f.name for f in fields(event)}
    assert not hasattr(event, 'reason')


@pytest.mark.parametrize('value', [True, False, -1, 1 << 63, '1', None, 1.0])
@pytest.mark.parametrize('field', ['generation', 'revision'])
def test_revision_input_refused_before_reads(repo, value, field):
    assert claim(repo, **{field: value}).unwrap_err() == 'invalid_readiness_revision'
    assert repo.order == [] and repo.log_mock.call_count == 0


@pytest.mark.parametrize('status', [s for s in TournamentStatus if s != TournamentStatus.ONGOING])
def test_lifecycle_refusal(repo, status):
    repo.tournament.tournament_status = status
    assert claim(repo).unwrap_err() == 'tournament_not_ongoing'
    assert repo.ready_writes == repo.log_mock.call_count == 0


def test_role_and_foreign_side_adversaries(repo, monkeypatch):
    for side, user in ((MatchSide.B, repo.users[0]), (MatchSide.A, uuid4())):
        assert claim(repo, side, user).unwrap_err() == 'readiness_forbidden'
    monkeypatch.setattr(service.authority.tournament_orga_service, 'is_orga_for_tournament', lambda user, tid: True)
    assert claim(repo, MatchSide.B, uuid4()).unwrap().actor_role == 'orga'


def test_current_global_orga(repo, monkeypatch):
    monkeypatch.setattr(service.authority, 'get_permissions_for_user', lambda user: {'lan_tournament.administrate'})
    assert claim(repo, MatchSide.B, uuid4()).unwrap().actor_role == 'orga'


@pytest.mark.parametrize('side', [None, True, 'a', 0])
def test_invalid_side_refused_before_queries(repo, side):
    assert claim(repo, side).unwrap_err() == 'invalid_match_side'
    assert repo.order == [] and repo.log_mock.call_count == 0


def test_bigint_exhaustion_preserves_state(repo):
    repo.match = replace(repo.match, readiness_revision=service.MAX_REVISION)
    assert claim(repo).unwrap_err() == 'readiness_revision_exhausted'
    assert repo.ready_writes == repo.log_mock.call_count == 0


@pytest.mark.parametrize('adversary,error', [
    ('confirmed', 'match_confirmed'), ('ffa', 'readiness_format_unsupported'),
    ('phase_ffa', 'readiness_format_unsupported'), ('missing', 'readiness_pairing_invalid'),
    ('foreign', 'readiness_pairing_invalid'), ('removed', 'readiness_pairing_invalid'),
    ('ended', 'readiness_pairing_invalid'), ('pointer', 'readiness_pairing_invalid'),
])
def test_format_and_pairing_adversaries(repo, adversary, error):
    if adversary == 'confirmed':
        repo.match = replace(repo.match, confirmed_by=repo.users[0])
    elif adversary == 'ffa':
        repo.tournament.game_format = GameFormat.FREE_FOR_ALL
    elif adversary == 'phase_ffa':
        repo.match = replace(repo.match, phase=2)
    elif adversary == 'missing':
        repo.contestants.pop()
    elif adversary == 'foreign':
        repo.participants[repo.pairing.side_a.id].tournament_id = uuid4()
    elif adversary == 'removed':
        repo.participants[repo.pairing.side_a.id].removed_at = NOW
    elif adversary == 'ended':
        repo.pairing = replace(repo.pairing, ended_at=NOW)
    else:
        repo.match = replace(repo.match, pairing_id=uuid4())
    assert claim(repo).unwrap_err() == error
    assert repo.ready_writes == repo.log_mock.call_count == 0


def test_audit_failure_requires_caller_rollback(repo):
    repo.log_mock.side_effect = RuntimeError('audit unavailable')
    assert claim(repo).unwrap_err() == 'readiness_audit_failed'
    assert repo.committed == 0


def test_tournament_first_fresh_order_and_inert_effects(repo, monkeypatch):
    send = Mock()
    monkeypatch.setattr(service.match_ready_claimed, 'send', send)
    change = claim(repo).unwrap()
    assert repo.order[:5] == ['identity', 'tournament', 'match', 'contestants', 'pairing']
    assert repo.order.index('participant_fresh') > repo.order.index('tournament')
    send.assert_not_called()
    assert service.dispatch_readiness_effects(change).is_ok()
    send.assert_called_once()
    repo.dispatch_mock.assert_called_once_with(repo.pending_ids)


def test_dispatch_continues_after_failed_listener(repo, monkeypatch, caplog):
    assert claim(repo).is_ok()
    change = claim(repo, MatchSide.B, repo.users[1]).unwrap()
    assert len(change.events) == 2 and change.match.both_ready_notified_at is None
    monkeypatch.setattr(service.match_ready_claimed, 'send', Mock(side_effect=RuntimeError('listener')))
    remaining = Mock()
    monkeypatch.setattr(service.match_both_ready, 'send', remaining)
    assert service.dispatch_readiness_effects(change).unwrap_err() == 'readiness_dispatch_failed'
    remaining.assert_called_once()
    repo.dispatch_mock.assert_called_once_with(repo.pending_ids)
    assert 'Post-commit readiness effect failed' in caplog.text


@pytest.mark.parametrize('operation', ['claim', 'revoke'])
def test_invitation_failure_requires_caller_rollback_without_effects(repo, operation):
    if operation == 'revoke':
        assert claim(repo).is_ok()
    repo.reconcile_mock.side_effect = None
    repo.reconcile_mock.return_value = Err('invitation_intent_failed')
    result = claim(repo) if operation == 'claim' else revoke(repo)
    assert result.unwrap_err() == 'invitation_intent_failed'
    assert repo.committed == 0
    repo.dispatch_mock.assert_not_called()


def test_reconciliation_coalesces_actual_typed_ids_without_dispatch(monkeypatch):
    from byceps.services.lan_tournament import tournament_invitation_service
    match_ids = tuple(sorted((uuid4(), uuid4()), key=str))
    pending_ids: tuple[MatchInvitationID, ...] = tuple(sorted(
        (MatchInvitationID(uuid4()), MatchInvitationID(uuid4())), key=str,
    ))
    reconcile = Mock(side_effect=[Ok((pending_ids[1],)), Ok(pending_ids)])
    dispatch = Mock()
    monkeypatch.setattr(tournament_invitation_service, 'reconcile_match_invitations_flush', reconcile)
    monkeypatch.setattr(tournament_invitation_service, 'dispatch_match_invitations', dispatch)
    result = service.reconcile_invitations_flush(
        (*reversed(match_ids), match_ids[0]), occurred_at=NOW,
    )
    assert result.unwrap() == pending_ids
    assert [c.args[0] for c in reconcile.call_args_list] == list(match_ids)
    assert all(c.kwargs == {'occurred_at': NOW} for c in reconcile.call_args_list)
    dispatch.assert_not_called()
