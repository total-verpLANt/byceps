"""
tests.integration.services.lan_tournament.test_tournament_create_audit_entry
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from datetime import datetime, UTC
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import text

from byceps.database import db
from byceps.services.lan_tournament import (
    tournament_repository,
    tournament_request_service,
    tournament_service,
)
from byceps.services.lan_tournament.models import ContestantType
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.party.models import PartyID


PARTY_ID = PartyID('lan-party-create-audit-entry')
EVENT_TYPE = 'tournament-created'
LOG_DATA = {'source': 'audit-test'}


@pytest.fixture(scope='module')
def party(make_party, brand):
    return make_party(brand, PARTY_ID, 'Create Audit Entry Party')


@pytest.fixture(scope='module')
def creator(make_user):
    return make_user('CreateAuditEntryCreator')


@pytest.fixture(scope='module')
def decider(make_user):
    return make_user('CreateAuditEntryDecider')


def _create(party, name, **kwargs):
    return tournament_service.create_tournament(
        party.id, name, contestant_type=ContestantType.SOLO, **kwargs
    )


def _accepted_request(party, creator, decider):
    request, _event = tournament_request_service.submit_request(
        party.id,
        creator.id,
        party_capacity=None,
        name=f'Audit Request Cup {uuid4()}',
        game='Rocket League',
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        team_size=1,
        participant_limit=8,
        preferred_start_time=datetime(2026, 10, 24, 18, 0, tzinfo=UTC),
        preferred_end_time=datetime(2026, 10, 24, 22, 0, tzinfo=UTC),
        description='A friendly cup.',
    ).unwrap()
    assert tournament_request_service.accept_request(
        request.id, decider.id
    ).is_ok()
    return request


# -- reads: only committed data, through a separate connection --


def _committed(sql: str, **params):
    with db.engine.connect() as connection:
        return connection.execute(text(sql), params).all()


def _committed_tournaments(name: str) -> list[tuple]:
    return _committed(
        'SELECT id FROM lan_tournaments WHERE name = :name', name=name
    )


def _committed_tournaments_by_id(tournament_id) -> list[tuple]:
    return _committed(
        'SELECT id FROM lan_tournaments WHERE id = :id', id=tournament_id
    )


def _committed_log_entries(tournament_id) -> list[tuple]:
    return _committed(
        'SELECT event_type, initiator_id, data'
        ' FROM lan_tournament_log_entries'
        ' WHERE tournament_id = :id',
        id=tournament_id,
    )


def test_create_with_log_event_stages_entry_in_the_same_commit(party, creator):
    name = f'Audit Cup {uuid4()}'
    real_create_log_entry = tournament_service.create_log_entry
    staged: dict = {}

    def spy(event_type, tournament_id, initiator_id, **kwargs):
        staged['kwargs'] = kwargs
        staged['committed_tournaments'] = _committed_tournaments_by_id(
            tournament_id
        )
        return real_create_log_entry(
            event_type, tournament_id, initiator_id, **kwargs
        )

    with (
        patch.object(tournament_service, 'create_log_entry', side_effect=spy),
        patch.object(
            tournament_repository,
            'commit_session',
            wraps=tournament_repository.commit_session,
        ) as commit_session,
    ):
        result = _create(
            party,
            name,
            initiator_id=creator.id,
            log_event_type=EVENT_TYPE,
            log_data=LOG_DATA,
        )

    assert result.is_ok()
    tournament, _event = result.unwrap()
    assert staged['kwargs']['commit'] is False
    assert staged['committed_tournaments'] == []
    assert commit_session.call_count == 1
    assert _committed_tournaments_by_id(tournament.id) == [(tournament.id,)]
    assert _committed_log_entries(tournament.id) == [
        (EVENT_TYPE, creator.id, LOG_DATA)
    ]


def test_create_with_log_event_requires_initiator(party):
    name = f'Audit Cup {uuid4()}'

    with pytest.raises(
        ValueError,
        match='initiator_id is required when log_event_type is set',
    ):
        _create(party, name, log_event_type=EVENT_TYPE, log_data=LOG_DATA)

    assert _committed_tournaments(name) == []


def test_create_without_log_event_writes_no_entry(party, creator):
    name = f'Audit Cup {uuid4()}'

    result = _create(party, name, initiator_id=creator.id)

    assert result.is_ok()
    tournament, _event = result.unwrap()
    assert _committed_tournaments_by_id(tournament.id) == [(tournament.id,)]
    assert _committed_log_entries(tournament.id) == []


def test_create_log_failure_rolls_back_the_tournament(party, creator):
    name = f'Audit Cup {uuid4()}'

    with (
        patch.object(
            tournament_service,
            'create_log_entry',
            side_effect=RuntimeError('log write failed'),
        ),
        patch.object(
            tournament_repository,
            'rollback_session',
            wraps=tournament_repository.rollback_session,
        ) as rollback_session,
    ):
        with pytest.raises(RuntimeError, match='log write failed'):
            _create(
                party,
                name,
                initiator_id=creator.id,
                log_event_type=EVENT_TYPE,
                log_data=LOG_DATA,
            )

    assert rollback_session.call_count == 1
    assert _committed_tournaments(name) == []
    # The flushed row is gone from the caller's session as well.
    assert (
        db.session.execute(
            text('SELECT id FROM lan_tournaments WHERE name = :name'),
            {'name': name},
        ).all()
        == []
    )


def test_request_linked_create_still_commits_once(party, creator, decider):
    request = _accepted_request(party, creator, decider)
    name = f'Audit Request Link Cup {uuid4()}'

    with patch.object(
        tournament_repository,
        'commit_session',
        wraps=tournament_repository.commit_session,
    ) as commit_session:
        result = _create(
            party,
            name,
            created_from_request_id=request.id,
            initiator_id=decider.id,
        )

    assert result.is_ok()
    tournament, _event = result.unwrap()
    assert commit_session.call_count == 1
    assert _committed(
        'SELECT created_from_request_id FROM lan_tournaments WHERE id = :id',
        id=tournament.id,
    ) == [(request.id,)]


def test_request_linked_create_with_log_event_commits_once(
    party, creator, decider
):
    request = _accepted_request(party, creator, decider)
    name = f'Audit Request Link Log Cup {uuid4()}'

    with patch.object(
        tournament_repository,
        'commit_session',
        wraps=tournament_repository.commit_session,
    ) as commit_session:
        result = _create(
            party,
            name,
            created_from_request_id=request.id,
            initiator_id=decider.id,
            log_event_type=EVENT_TYPE,
            log_data=LOG_DATA,
        )

    assert result.is_ok()
    tournament, _event = result.unwrap()
    assert commit_session.call_count == 1
    assert (
        EVENT_TYPE,
        decider.id,
        LOG_DATA,
    ) in _committed_log_entries(tournament.id)
