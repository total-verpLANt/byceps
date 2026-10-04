"""Unit coverage for deterministic, transaction-scoped request quota locks."""

from hashlib import sha256
import json
from unittest.mock import call, patch
from uuid import UUID

import pytest
from sqlalchemy.sql.elements import TextClause

from byceps.services.lan_tournament import tournament_request_repository
from byceps.services.party.models import PartyID
from byceps.services.user.models import UserID


MOCK_PREFIX = 'byceps.services.lan_tournament.tournament_request_repository'
PROPOSER_ID = UserID(UUID('00000000-0000-0000-0000-000000000001'))
OTHER_PROPOSER_ID = UserID(UUID('00000000-0000-0000-0000-000000000002'))


# fmt: off
@pytest.mark.parametrize('party_id', [
    PartyID('lan-party'),
    PartyID('party,"\\line\n'),
    PartyID('München-🎮'),
])
# fmt: on
def test_request_quota_lock_key_is_stable_and_signed_64_bit(party_id):
    payload = json.dumps(
        ['lan_tournament.request_quota.v1', str(party_id), str(PROPOSER_ID)],
        separators=(',', ':'),
    ).encode('utf-8')
    expected = int.from_bytes(sha256(payload).digest()[:8], 'big', signed=True)

    key = tournament_request_repository._request_quota_lock_key(
        party_id, PROPOSER_ID
    )

    assert type(key) is int
    assert -(2**63) <= key <= 2**63 - 1
    assert key == expected
    assert tournament_request_repository._request_quota_lock_key(
        PartyID(str(party_id)), UserID(UUID(str(PROPOSER_ID)))
    ) == key


def test_request_quota_lock_keys_separate_parties_and_proposers():
    scopes = [
        (PartyID('lan-party'), PROPOSER_ID),
        (PartyID('other-party'), PROPOSER_ID),
        (PartyID('lan-party'), OTHER_PROPOSER_ID),
        (PartyID('other-party'), OTHER_PROPOSER_ID),
        (PartyID(f'lan-party,{PROPOSER_ID}'), OTHER_PROPOSER_ID),
    ]

    keys = [
        tournament_request_repository._request_quota_lock_key(party, proposer)
        for party, proposer in scopes
    ]

    assert len(keys) == len(scopes)
    assert len(set(keys)) == len(scopes)


@patch(f'{MOCK_PREFIX}.json.dumps', wraps=json.dumps)
def test_request_quota_lock_key_uses_canonical_namespaced_json(mock_dumps):
    party_id = PartyID('party,"\\line\n')

    tournament_request_repository._request_quota_lock_key(party_id, PROPOSER_ID)

    mock_dumps.assert_called_once_with(
        ['lan_tournament.request_quota.v1', str(party_id), str(PROPOSER_ID)],
        separators=(',', ':'),
    )


# fmt: off
@pytest.mark.parametrize(('prefix', 'expected'), [
    (bytes.fromhex('0000000000000000'), 0),
    (bytes.fromhex('7fffffffffffffff'), 2**63 - 1),
    (bytes.fromhex('8000000000000000'), -(2**63)),
    (bytes.fromhex('ffffffffffffffff'), -1),
])
# fmt: on
@patch(f'{MOCK_PREFIX}.sha256')
def test_request_quota_lock_key_interprets_first_eight_bytes_as_signed_big_endian(
    mock_sha256, prefix, expected
):
    mock_sha256.return_value.digest.return_value = prefix + b'\x01' * 24

    key = tournament_request_repository._request_quota_lock_key(
        PartyID('lan-party'), PROPOSER_ID
    )

    assert key == expected
    mock_sha256.assert_called_once_with(
        b'["lan_tournament.request_quota.v1","lan-party",'
        b'"00000000-0000-0000-0000-000000000001"]'
    )


@patch(f'{MOCK_PREFIX}.db')
def test_request_quota_lock_uses_transaction_scoped_parameterized_sql(mock_db):
    party_id = PartyID("untrusted-party'); SELECT 1; --")
    key = tournament_request_repository._request_quota_lock_key(
        party_id, PROPOSER_ID
    )

    tournament_request_repository.lock_request_quota_for_update(
        party_id, PROPOSER_ID
    )

    mock_db.session.execute.assert_called_once()
    args, kwargs = mock_db.session.execute.call_args
    statement, parameters = args
    assert isinstance(statement, TextClause)
    assert str(statement) == 'SELECT pg_advisory_xact_lock(:key)'
    assert parameters == {'key': key}
    assert kwargs == {}
    assert mock_db.session.mock_calls == [call.execute(statement, {'key': key})]
    mock_db.session.commit.assert_not_called()
    mock_db.session.rollback.assert_not_called()
    mock_db.session.flush.assert_not_called()
