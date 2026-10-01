"""
tests.integration.services.lan_tournament.test_tournament_update_legacy_values
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Stored values that the create rules now refuse must not lock out an
edit that leaves them alone, while a changed value is still checked.
"""

import json
from uuid import uuid4

import pytest
from sqlalchemy import update

from byceps.database import db
from byceps.services.lan_tournament import tournament_service
from byceps.services.lan_tournament.dbmodels.tournament import DbTournament
from byceps.services.lan_tournament.models.contestant_type import (
    ContestantType,
)
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.party.models import PartyID

from tests.helpers import log_in_user


BASE_URL = 'http://admin.acmecon.test/lan-tournaments'

PARTY_ID = PartyID('lan-party-update-legacy')

FLOOR_MESSAGE = 'Whole numbers from 1 only.'


@pytest.fixture(scope='module')
def party(make_party, brand):
    return make_party(brand, PARTY_ID, 'Update Legacy Party')


@pytest.fixture(scope='module')
def admin(make_admin):
    user = make_admin({'admin.access', 'lan_tournament.update'})
    log_in_user(user.id)
    return user


@pytest.fixture(scope='module')
def client(make_client, admin_app, admin):
    return make_client(admin_app, user_id=admin.id)


def _name(label: str) -> str:
    return f'{label} {uuid4().hex[:8]}'


def _set_row(tournament_id, **values) -> None:
    db.session.execute(
        update(DbTournament)
        .where(DbTournament.id == tournament_id)
        .values(**values)
    )
    db.session.commit()


def _solo(party, name, *, status='DRAFT', **row):
    tournament, _ = tournament_service.create_tournament(
        party.id,
        name,
        contestant_type=ContestantType.SOLO,
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        min_players=4,
        max_players=16,
    ).unwrap()
    _set_row(tournament.id, tournament_status=status, **row)
    return tournament


def _ffa(party, name, *, status='DRAFT', table):
    tournament, _ = tournament_service.create_tournament(
        party.id,
        name,
        contestant_type=ContestantType.SOLO,
        game_format=GameFormat.FREE_FOR_ALL,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        point_table=[3, 2, 1],
        group_size_min=2,
        group_size_max=4,
        advancement_count=1,
    ).unwrap()
    _set_row(
        tournament.id,
        tournament_status=status,
        point_table=json.dumps(table),
    )
    return tournament


def _stored(tournament_id) -> DbTournament:
    db.session.expire_all()
    return db.session.get(DbTournament, tournament_id)


def _solo_form(name, **extra) -> dict:
    return {
        'category': 'MAIN',
        'name': name,
        'contestant_type': 'SOLO',
        'game_format': 'ONE_V_ONE',
        'elimination_mode': 'SINGLE_ELIMINATION',
        **extra,
    }


def _ffa_form(name, table, **extra) -> dict:
    return {
        'category': 'MAIN',
        'name': name,
        'contestant_type': 'SOLO',
        'game_format': 'FREE_FOR_ALL',
        'elimination_mode': 'SINGLE_ELIMINATION',
        'point_table': ', '.join(str(v) for v in table),
        'group_size_min': '2',
        'group_size_max': '4',
        'advancement_count': '1',
        **extra,
    }


def _post(client, tournament_id, data):
    return client.post(f'{BASE_URL}/tournaments/{tournament_id}', data=data)


def test_locked_legacy_zero_minimum_accepts_description(client, party):
    name = _name('Locked zero')
    t = _solo(party, name, status='ONGOING', min_players=0)

    response = _post(
        client,
        t.id,
        _solo_form(name, min_players='0', max_players='16', description='New'),
    )

    assert response.status_code == 302
    assert _stored(t.id).description == 'New'


def test_locked_legacy_65_place_table_accepts_description(client, party):
    name = _name('Locked 65')
    table = list(range(65, 0, -1))
    t = _ffa(party, name, status='ONGOING', table=table)

    response = _post(client, t.id, _ffa_form(name, table, description='New'))

    assert response.status_code == 302
    assert _stored(t.id).description == 'New'


def test_draft_unchanged_legacy_zero_saves(client, party):
    name = _name('Draft zero')
    t = _solo(party, name, min_players=0, min_teams=0)

    response = _post(
        client,
        t.id,
        _solo_form(
            name,
            min_players='0',
            max_players='16',
            min_teams='0',
            description='New',
        ),
    )

    assert response.status_code == 302
    stored = _stored(t.id)
    assert stored.description == 'New'
    assert stored.min_players == 0


def test_draft_changing_minimum_to_zero_is_rejected(client, party):
    name = _name('Draft to zero')
    t = _solo(party, name)

    response = _post(
        client,
        t.id,
        _solo_form(name, min_players='0', max_players='16', description='X'),
    )

    assert response.status_code == 200
    assert FLOOR_MESSAGE in response.get_data(as_text=True)
    stored = _stored(t.id)
    assert stored.min_players == 4
    assert stored.description is None


def test_draft_minimum_above_unchanged_maximum_is_rejected(client, party):
    name = _name('Draft min above')
    t = _solo(party, name)

    response = _post(
        client,
        t.id,
        _solo_form(name, min_players='20', max_players='16'),
    )

    assert response.status_code == 200
    assert 'Must be at least' in response.get_data(as_text=True)
    assert _stored(t.id).min_players == 4


def test_draft_oversized_count_is_a_field_error(client, party):
    name = _name('Draft huge')
    t = _solo(party, name)

    response = _post(
        client,
        t.id,
        _solo_form(name, min_players='4', max_players='3000000000'),
    )

    assert response.status_code == 200
    assert 'At most' in response.get_data(as_text=True)
    assert _stored(t.id).max_players == 16


def test_draft_unparseable_count_is_not_mistaken_for_unset(client, party):
    name = _name('Draft junk')
    t = _solo(party, name, min_players=None)

    response = _post(
        client,
        t.id,
        _solo_form(name, min_players='abc', max_players='16'),
    )

    assert response.status_code == 200
    assert FLOOR_MESSAGE in response.get_data(as_text=True)


def test_draft_changed_legacy_table_is_still_capped(client, party):
    name = _name('Draft 65 changed')
    table = list(range(65, 0, -1))
    t = _ffa(party, name, table=table)

    response = _post(client, t.id, _ffa_form(name, list(range(66, 1, -1))))

    assert response.status_code == 200
    assert 'At most 64 places.' in response.get_data(as_text=True)
    assert json.loads(_stored(t.id).point_table) == table


def test_draft_unchanged_legacy_table_saves(client, party):
    name = _name('Draft 65 same')
    table = list(range(65, 0, -1))
    t = _ffa(party, name, table=table)

    response = _post(client, t.id, _ffa_form(name, table, description='New'))

    assert response.status_code == 302
    assert _stored(t.id).description == 'New'
