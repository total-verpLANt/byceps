"""
tests.integration.services.lan_tournament.test_opponent_ready_mail
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from byceps.database import db
from byceps.services.lan_tournament import (
    tournament_match_service as facade,
    tournament_notification_service as notifications,
    tournament_readiness_service as service,
    tournament_repository as repo,
)
from byceps.services.lan_tournament.dbmodels.match import DbTournamentMatch
from byceps.services.lan_tournament.dbmodels.match_contestant import (
    DbTournamentMatchToContestant,
)
from byceps.services.lan_tournament.dbmodels.participant import (
    DbTournamentParticipant,
)
from byceps.services.lan_tournament.dbmodels.tournament import DbTournament
from byceps.services.lan_tournament.events import MatchBothReadyEvent
from byceps.services.lan_tournament.models.match_readiness import (
    ContestantIdentity,
)
from byceps.services.lan_tournament.models.tournament_match import MatchSide
from byceps.services.party.models import PartyID
from byceps.util.result import Ok
from byceps.util.uuid import uuid7


NOW = datetime(2026, 10, 17, 12)


@pytest.fixture(scope='module')
def party(make_party, brand):
    return make_party(
        brand, PartyID('iqya3-opponent-ready-mail'), 'Opponent ready mail'
    )


@pytest.fixture(scope='module')
def users(make_user):
    return [make_user(f'IqyaOpponentMail{i}') for i in range(3)]


@pytest.fixture
def setup(party, users):
    tournament = DbTournament(
        uuid7(),
        party.id,
        f'Opponent mail {uuid7()}',
        NOW,
        game_format='ONE_V_ONE',
        tournament_status='ONGOING',
    )
    db.session.add(tournament)
    db.session.flush()
    participants = [
        DbTournamentParticipant(uuid7(), user.id, tournament.id, NOW)
        for user in users
    ]
    db.session.add_all(participants)
    match = DbTournamentMatch(uuid7(), tournament.id, NOW)
    db.session.add(match)
    db.session.flush()
    contestants = [
        DbTournamentMatchToContestant(
            uuid7(), match.id, NOW, participant_id=p.id
        )
        for p in participants[:2]
    ]
    db.session.add_all(contestants)
    db.session.commit()
    match_id, tournament_id = match.id, tournament.id
    assert service.refresh_pairing_and_invitations_flush(
        match_id, occurred_at=NOW
    ).is_ok()
    db.session.commit()
    yield SimpleNamespace(
        match_id=match_id,
        tournament_id=tournament_id,
        participants=participants,
        contestants=contestants,
    )
    db.session.rollback()


@pytest.fixture
def calls(monkeypatch):
    """Record mail and invitation dispatch calls in their call order."""
    recorded = SimpleNamespace(mails=[], order=[])

    def mail(tournament_id, match_id, ready):
        recorded.mails.append((tournament_id, match_id, ready))
        recorded.order.append('mail')

    def invitations(invitation_ids):
        recorded.order.append('invitations')
        return Ok(None)

    monkeypatch.setattr(notifications, 'send_opponent_ready_emails', mail)
    monkeypatch.setattr(service, 'dispatch_pending_invitations', invitations)
    return recorded


def actor(setup, side):
    """Return the user who holds `side` in the current pairing."""
    pairing = repo.get_match_pairing(setup.match_id)
    identity = pairing.side_a if side == MatchSide.A else pairing.side_b
    return next(p.user_id for p in setup.participants if p.id == identity.id)


def identity_of(setup, side):
    pairing = repo.get_match_pairing(setup.match_id)
    return pairing.side_a if side == MatchSide.A else pairing.side_b


def claim(setup, side=MatchSide.A):
    """Claim and commit; the caller dispatches."""
    match = repo.get_match(setup.match_id)
    change = facade.claim_ready(
        match.id,
        side,
        actor(setup, side),
        expected_pairing_generation=match.pairing_generation,
        expected_readiness_revision=match.readiness_revision,
    ).unwrap()
    repo.commit_session()
    return change


def claim_and_dispatch(setup, side=MatchSide.A):
    change = claim(setup, side)
    assert facade.dispatch_readiness_effects(change).is_ok()
    return change


def revoke_and_dispatch(setup, side=MatchSide.A):
    match = repo.get_match(setup.match_id)
    change = facade.revoke_ready(
        match.id,
        side,
        actor(setup, side),
        expected_pairing_generation=match.pairing_generation,
        expected_readiness_revision=match.readiness_revision,
    ).unwrap()
    repo.commit_session()
    assert facade.dispatch_readiness_effects(change).is_ok()
    return change


def test_first_claim_mails_the_opponent_once(setup, calls):
    change = claim_and_dispatch(setup)

    assert change.events[0].first_claim is True
    assert calls.mails == [
        (
            setup.tournament_id,
            setup.match_id,
            ContestantIdentity(
                kind='participant', id=identity_of(setup, MatchSide.A).id
            ),
        )
    ]
    # The mail goes out before the durable invitation dispatch.
    assert calls.order == ['mail', 'invitations']


def test_reclaim_after_revoke_sends_no_second_mail(setup, calls):
    claim_and_dispatch(setup)
    revoke_and_dispatch(setup)
    reclaim = claim_and_dispatch(setup)

    assert reclaim.events[0].first_claim is False
    assert len(calls.mails) == 1


def test_each_side_mails_once(setup, calls):
    claim_and_dispatch(setup, MatchSide.A)
    revoke_and_dispatch(setup, MatchSide.A)
    claim_and_dispatch(setup, MatchSide.B)
    revoke_and_dispatch(setup, MatchSide.B)
    claim_and_dispatch(setup, MatchSide.A)

    assert [ready for _, _, ready in calls.mails] == [
        identity_of(setup, MatchSide.A),
        identity_of(setup, MatchSide.B),
    ]


def test_claim_that_makes_both_ready_mails_nobody(setup, calls):
    claim_and_dispatch(setup, MatchSide.A)
    completing = claim_and_dispatch(setup, MatchSide.B)

    assert completing.events[0].first_claim is True
    assert isinstance(completing.events[1], MatchBothReadyEvent)
    assert [ready for _, _, ready in calls.mails] == [
        identity_of(setup, MatchSide.A),
    ]


def test_new_pairing_generation_mails_again(setup, calls):
    first = claim_and_dispatch(setup)
    assert len(calls.mails) == 1

    # Seat a new opponent in the first slot; the production refresh path
    # retires the pairing and bumps its generation.
    setup.contestants[0].participant_id = setup.participants[2].id
    db.session.flush()
    refreshed = service.refresh_pairing_and_invitations_flush(
        setup.match_id, occurred_at=NOW + timedelta(minutes=1)
    ).unwrap()
    repo.commit_session()
    assert (
        refreshed.match.pairing_generation == first.match.pairing_generation + 1
    )

    second = claim_and_dispatch(setup)

    assert second.events[0].first_claim is True
    assert len(calls.mails) == 2
    assert calls.mails[1][2] == identity_of(setup, MatchSide.A)
    assert calls.mails[1][2] != calls.mails[0][2]


def test_mail_failure_keeps_dispatch_ok(setup, calls, monkeypatch, caplog):
    spy = Mock(side_effect=RuntimeError('smtp down'))
    monkeypatch.setattr(notifications, 'send_opponent_ready_emails', spy)
    change = claim(setup)

    result = facade.dispatch_readiness_effects(change)

    assert result.is_ok()
    spy.assert_called_once()
    # The durable invitation dispatch still ran after the failed mail.
    assert calls.order == ['invitations']
    assert 'opponent-ready mail failed' in caplog.text
