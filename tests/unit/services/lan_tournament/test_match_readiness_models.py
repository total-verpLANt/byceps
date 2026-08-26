from dataclasses import FrozenInstanceError, fields
from datetime import datetime
from pathlib import Path
import sqlite3
from typing import get_type_hints
from uuid import UUID

import pytest
from sqlalchemy import BigInteger, CheckConstraint, DateTime, UniqueConstraint
from sqlalchemy.orm import configure_mappers

from byceps.cli.commands.create_database_tables import (
    _collect_dbmodel_paths,
    _load_dbmodels,
)
from byceps.database import db
from byceps.services.lan_tournament.dbmodels.match import DbTournamentMatch
from byceps.services.lan_tournament.dbmodels.match_readiness import (
    DbMatchInvitation,
    DbMatchPairing,
)
from byceps.services.lan_tournament.models.match_readiness import (
    ContestantIdentity,
    InvitationStatus,
    MatchInvitation,
    MatchPairing,
)
from byceps.services.lan_tournament.models.tournament import TournamentID
from byceps.services.lan_tournament.models.tournament_match import (
    MatchInvitationID,
    MatchPairingID,
    TournamentMatch,
    TournamentMatchID,
)
from byceps.services.user.models import UserID

from tests.helpers import generate_uuid


NOW = datetime(2026, 10, 5, 12)


def make_facts():
    match_id = TournamentMatchID(generate_uuid())
    tournament_id = TournamentID(generate_uuid())
    side_a = ContestantIdentity(kind='participant', id=generate_uuid())
    side_b = ContestantIdentity(kind='team', id=generate_uuid())
    pairing = MatchPairing(
        id=MatchPairingID(generate_uuid()),
        match_id=match_id,
        tournament_id=tournament_id,
        generation=2**40,
        side_a=side_a,
        side_b=side_b,
    )
    invitation = MatchInvitation(
        id=MatchInvitationID(generate_uuid()),
        match_id=match_id,
        tournament_id=tournament_id,
        pairing_generation=pairing.generation,
        recipient_id=UserID(generate_uuid()),
        status=InvitationStatus.PENDING,
        expected_readiness_revision=2**40,
    )
    return pairing, invitation


def test_readiness_facts_are_frozen():
    pairing, invitation = make_facts()
    for fact in (pairing.side_a, pairing.side_b, pairing, invitation):
        assert fact.__dataclass_params__.frozen
        field = fields(fact)[0]
        with pytest.raises(FrozenInstanceError):
            setattr(fact, field.name, getattr(fact, field.name))
    assert pairing.started_at is None
    assert pairing.ended_at is None
    assert invitation.attempts == 0
    for field in (
        'dispatch_token', 'next_attempt_at', 'lease_until', 'accepted_at', 'last_error'
    ):
        assert getattr(invitation, field) is None


def test_model_defaults_preserve_legacy_constructor():
    match_id = TournamentMatchID(generate_uuid())
    tournament_id = TournamentID(generate_uuid())
    dto = TournamentMatch(
        id=match_id,
        tournament_id=tournament_id,
        group_order=None,
        match_order=None,
        round=None,
        next_match_id=None,
        confirmed_by=None,
        created_at=NOW,
    )
    row = DbTournamentMatch(match_id, tournament_id, NOW)
    for match in (dto, row):
        assert match.pairing_generation == 0
        assert match.readiness_revision == 0
        assert match.pairing_id is None
        assert match.invitation_hold_a is False
        assert match.invitation_hold_b is False
        for name in (
            'occupied_since', 'ready_at_a', 'ready_at_b', 'ready_by_a',
            'ready_by_b', 'both_ready_notified_at',
        ):
            assert getattr(match, name) is None
    assert dto.__dataclass_params__.frozen
    with pytest.raises(FrozenInstanceError):
        dto.readiness_revision = 1
    for name in ('pairing_generation', 'readiness_revision'):
        column = DbTournamentMatch.__table__.c[name]
        assert column.default.arg == 0
        assert str(column.server_default.arg) == '0'
    for name in ('invitation_hold_a', 'invitation_hold_b'):
        column = DbTournamentMatch.__table__.c[name]
        assert column.default.arg is False
        assert str(column.server_default.arg) == 'false'


def test_pairing_and_invitation_enum_values():
    assert {status.value for status in InvitationStatus} == {
        'pending', 'dispatching', 'queued', 'sending', 'accepted', 'failed',
        'suppressed', 'delivery_unknown',
    }
    assert get_type_hints(ContestantIdentity) == {'kind': str, 'id': UUID}
    assert get_type_hints(MatchPairing) == {
        'id': MatchPairingID, 'match_id': TournamentMatchID,
        'tournament_id': TournamentID, 'generation': int,
        'side_a': ContestantIdentity, 'side_b': ContestantIdentity,
        'started_at': datetime | None, 'ended_at': datetime | None,
    }
    assert get_type_hints(MatchInvitation) == {
        'id': MatchInvitationID, 'match_id': TournamentMatchID,
        'tournament_id': TournamentID, 'pairing_generation': int,
        'recipient_id': UserID, 'status': InvitationStatus,
        'expected_readiness_revision': int, 'attempts': int,
        'dispatch_token': UUID | None, 'next_attempt_at': datetime | None,
        'lease_until': datetime | None, 'accepted_at': datetime | None,
        'last_error': str | None,
    }
    match_types = get_type_hints(TournamentMatch)
    assert match_types['pairing_generation'] is int
    assert match_types['readiness_revision'] is int
    assert match_types['pairing_id'] == MatchPairingID | None
    assert match_types['invitation_hold_a'] is bool
    assert match_types['invitation_hold_b'] is bool
    for id_type in (MatchPairingID, MatchInvitationID):
        assert id_type.__supertype__ is UUID
    for model, names in (
        (DbTournamentMatch, ('pairing_generation', 'readiness_revision')),
        (DbMatchPairing, ('generation',)),
        (DbMatchInvitation, ('pairing_generation', 'expected_readiness_revision')),
    ):
        for name in names:
            column = model.__table__.c[name]
            assert isinstance(column.type, BigInteger)
            assert not column.nullable
    for model in (DbMatchPairing, DbMatchInvitation):
        assert model.__table__.c.id.default.arg.__wrapped__.__name__ == 'generate_uuid7'
        for column in model.__table__.c:
            if isinstance(column.type, DateTime):
                assert not column.type.timezone
                assert column.nullable
    assert DbMatchInvitation.__table__.c.status.type.length == 16
    assert DbMatchInvitation.__table__.c.last_error.type.length == 500


def test_model_loader_discovers_readiness_tables():
    assert Path('byceps/services/lan_tournament/dbmodels/match_readiness.py') in set(
        _collect_dbmodel_paths()
    )
    _load_dbmodels()
    configure_mappers()
    assert db.metadata.tables['lan_tournament_match_pairings'] is DbMatchPairing.__table__
    assert db.metadata.tables['lan_tournament_match_invitations'] is DbMatchInvitation.__table__
    relationship = DbTournamentMatch.__mapper__.relationships['confirmed_by_user']
    assert relationship.local_columns == {DbTournamentMatch.__table__.c.confirmed_by}


def test_history_work_metadata_retains_snapshots_and_unique_keys():
    pair_table = DbMatchPairing.__table__
    invitation_table = DbMatchInvitation.__table__
    assert not pair_table.foreign_keys
    assert {(fk.parent.name, fk.target_fullname) for fk in invitation_table.foreign_keys} == {
        ('recipient_id', 'users.id')
    }
    for table, name, columns in (
        (pair_table, 'uq_lan_tournament_match_pairings_match_generation',
         ('match_id', 'generation')),
        (invitation_table,
         'uq_lan_tournament_match_invitations_match_generation_recipient',
         ('match_id', 'pairing_generation', 'recipient_id')),
    ):
        unique = next(c for c in table.constraints if c.name == name)
        assert isinstance(unique, UniqueConstraint)
        assert tuple(unique.columns.keys()) == columns
    for name in ('side_a_kind', 'side_b_kind', 'side_a_id', 'side_b_id'):
        assert not pair_table.c[name].nullable
    assert pair_table.c.side_a_kind.type.length == 11
    assert pair_table.c.side_b_kind.type.length == 11
    index, = invitation_table.indexes
    assert index.name == 'ix_lan_tournament_match_invitations_status_next_attempt'
    assert tuple(index.columns.keys()) == ('status', 'next_attempt_at')
    fk, = DbTournamentMatch.__table__.c.pairing_id.foreign_keys
    assert fk.name == 'fk_lan_tournament_matches_pairing_id'
    assert fk.target_fullname == 'lan_tournament_match_pairings.id'
    assert fk.ondelete is None


def evaluate_check(model, suffix, values):
    """Evaluate the actual portable CHECK expression without a database fixture."""
    check = next(
        c for c in model.__table__.constraints
        if isinstance(c, CheckConstraint) and c.name.endswith('_' + suffix)
    )
    columns = ', '.join(f':{name} AS {name}' for name in values)
    with sqlite3.connect(':memory:') as connection:
        # Only owned schema expressions/column names are interpolated; values bind.
        return connection.execute(
            f'SELECT ({check.sqltext}) FROM (SELECT {columns})', values  # noqa: S608
        ).fetchone()[0]


# fmt: off
@pytest.mark.parametrize(('kind', 'valid'), [
    ('participant', True), ('team', True), ('PARTICIPANT', False),
    ('placeholder', False), ('', False),
])
# fmt: on
def test_pairing_kind_checks(kind, valid):
    for side in ('a', 'b'):
        name = f'side_{side}_kind'
        assert bool(evaluate_check(DbMatchPairing, name, {name: kind})) is valid


def test_pairing_identity_time_and_counter_checks():
    for same_kind, same_id, valid in (
        (True, True, False), (True, False, True), (False, True, True),
    ):
        assert bool(evaluate_check(DbMatchPairing, 'distinct_sides', {
            'side_a_kind': 'participant',
            'side_b_kind': 'participant' if same_kind else 'team',
            'side_a_id': 'a', 'side_b_id': 'a' if same_id else 'b',
        })) is valid
    for start, end, valid in (
        (None, None, True), (None, 1, True), (1, None, True),
        (1, 1, True), (1, 2, True), (2, 1, False),
    ):
        assert bool(evaluate_check(DbMatchPairing, 'time_order', {
            'started_at': start, 'ended_at': end,
        })) is valid
    for model, name, suffix in (
        (DbTournamentMatch, 'pairing_generation', 'pairing_generation'),
        (DbTournamentMatch, 'readiness_revision', 'readiness_revision'),
        (DbMatchPairing, 'generation', 'generation'),
        (DbMatchInvitation, 'pairing_generation', 'pairing_generation'),
        (DbMatchInvitation, 'expected_readiness_revision', 'readiness_revision'),
        (DbMatchInvitation, 'attempts', 'attempts'),
    ):
        for value in (-1, 0, 2**40):
            assert bool(evaluate_check(model, suffix, {name: value})) is (value >= 0)


@pytest.mark.parametrize('status', [s.value for s in InvitationStatus] + ['PENDING', 'bogus'])
def test_invitation_stage_checks(status):
    assert bool(evaluate_check(DbMatchInvitation, 'status', {'status': status})) is (
        status in {s.value for s in InvitationStatus}
    )
    active = status in {'dispatching', 'queued', 'sending'}
    for token in (None, 'token'):
        for lease in (None, 120):
            assert bool(evaluate_check(DbMatchInvitation, 'dispatch_stage', {
                'status': status, 'dispatch_token': token, 'lease_until': lease,
            })) is ((token is not None and lease is not None) if active else lease is None)
    for accepted_at in (None, 1):
        assert bool(evaluate_check(DbMatchInvitation, 'acceptance_stage', {
            'status': status, 'accepted_at': accepted_at,
        })) is ((accepted_at is not None) == (status == 'accepted'))


def test_persistence_constructors_round_trip_facts():
    pairing, invitation = make_facts()
    pair_row = DbMatchPairing(
        pairing.id, pairing.match_id, pairing.tournament_id, pairing.generation,
        pairing.side_a.kind, pairing.side_a.id, pairing.side_b.kind, pairing.side_b.id,
        started_at=NOW,
    )
    assert pair_row.id == pairing.id
    assert pair_row.generation == pairing.generation
    assert pair_row.side_a_kind == pairing.side_a.kind
    assert pair_row.side_a_id == pairing.side_a.id
    assert pair_row.side_b_kind == pairing.side_b.kind
    assert pair_row.side_b_id == pairing.side_b.id
    assert pair_row.started_at == NOW
    assert pair_row.ended_at is None
    row = DbMatchInvitation(
        invitation.id, invitation.match_id, invitation.tournament_id,
        invitation.pairing_generation, invitation.recipient_id, invitation.status.value,
        invitation.expected_readiness_revision,
    )
    for field in fields(invitation):
        expected = getattr(invitation, field.name)
        assert getattr(row, field.name) == (
            expected.value if isinstance(expected, InvitationStatus) else expected
        )
