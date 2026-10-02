"""
tests.integration.services.lan_tournament.test_seeding_repository
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from datetime import datetime, UTC
from itertools import count

import pytest
from sqlalchemy.exc import IntegrityError

from byceps.database import db
from byceps.services.lan_tournament import (
    tournament_seeding_repository,
    tournament_service,
)
from byceps.services.lan_tournament.dbmodels.seeding import DbTournamentSeeding
from byceps.services.lan_tournament.models import ContestantType
from byceps.services.lan_tournament.models.tournament_seeding import (
    RosterEntry,
    TournamentSeeding,
    TournamentSeedingID,
)
from byceps.services.party.models import PartyID
from byceps.util.uuid import generate_uuid7


PARTY_ID = PartyID('lan-party-2024-seeding-repo')

STALE_ERR = 'The seeding was changed by another orga. Reload the page.'

_counter = count(1)

SNAPSHOT = (RosterEntry('a', 'Alpha'), RosterEntry('b', 'Bravo'))


@pytest.fixture(scope='module')
def party(make_party, make_brand):
    brand = make_brand('seedingrepobrand', 'Seeding Repository Brand')
    return make_party(brand, PARTY_ID, 'LAN Party 2024 Seeding Repository')


@pytest.fixture(scope='module')
def orga(make_user):
    return make_user('SeedingRepoOrga')


@pytest.fixture
def tournament(party):
    result = tournament_service.create_tournament(
        PARTY_ID,
        f'Seeding Repository Tournament {next(_counter)}',
        contestant_type=ContestantType.SOLO,
    )
    assert result.is_ok()
    tournament, _ = result.unwrap()
    yield tournament
    db.session.rollback()
    tournament_seeding_repository.delete_seedings_for_tournament(tournament.id)
    db.session.commit()


def _make_seeding(tournament, target='initial', seed_code='a b c', **kwargs):
    now = datetime.now(UTC).replace(tzinfo=None)
    fields = {
        'id': TournamentSeedingID(generate_uuid7()),
        'tournament_id': tournament.id,
        'target': target,
        'seed_code': seed_code,
        'version': 1,
        'generated_seed_code': None,
        'generated_at': None,
        'updated_by': None,
        'created_at': now,
        'updated_at': now,
    }
    fields.update(kwargs)
    return TournamentSeeding(**fields)


def test_create_and_find_seeding(tournament, orga):
    seeding = _make_seeding(tournament, updated_by=orga.id)

    tournament_seeding_repository.create_seeding(seeding)

    found = tournament_seeding_repository.find_seeding(tournament.id, 'initial')
    assert found == seeding
    assert (
        tournament_seeding_repository.find_seeding(tournament.id, 'playoff')
        is None
    )
    assert tournament_seeding_repository.get_seedings_for_tournament(
        tournament.id
    ) == [seeding]
    assert (
        tournament_seeding_repository.find_seeding_for_update(
            tournament.id, 'initial'
        )
        == seeding
    )


def test_update_seeding_code_bumps_version(tournament, orga):
    seeding = _make_seeding(tournament)
    tournament_seeding_repository.create_seeding(seeding)
    later = datetime.now(UTC).replace(tzinfo=None)

    result = tournament_seeding_repository.update_seeding_code(
        seeding.id,
        seed_code='c b a',
        expected_version=1,
        roster_snapshot=SNAPSHOT,
        updated_by=orga.id,
        now=later,
    )

    assert result.is_ok()
    updated = result.unwrap()
    assert updated.version == 2
    assert updated.seed_code == 'c b a'
    assert updated.updated_by == orga.id
    assert updated.updated_at == later
    assert updated.created_at == seeding.created_at
    assert (
        tournament_seeding_repository.find_seeding(tournament.id, 'initial')
        == updated
    )


def test_update_seeding_code_stale_version_errs(tournament, orga):
    seeding = _make_seeding(tournament)
    tournament_seeding_repository.create_seeding(seeding)
    now = datetime.now(UTC).replace(tzinfo=None)
    first = tournament_seeding_repository.update_seeding_code(
        seeding.id,
        seed_code='first',
        expected_version=1,
        roster_snapshot=SNAPSHOT,
        updated_by=orga.id,
        now=now,
    )
    assert first.is_ok()

    stale = tournament_seeding_repository.update_seeding_code(
        seeding.id,
        seed_code='second',
        expected_version=1,
        roster_snapshot=SNAPSHOT,
        updated_by=orga.id,
        now=now,
    )

    assert stale.is_err()
    assert stale.unwrap_err() == STALE_ERR
    stored = tournament_seeding_repository.find_seeding(
        tournament.id, 'initial'
    )
    assert stored.seed_code == 'first'
    assert stored.version == 2


def test_unique_target_per_tournament(tournament):
    tournament_seeding_repository.create_seeding(_make_seeding(tournament))

    with pytest.raises(IntegrityError) as exc_info:
        tournament_seeding_repository.create_seeding(
            _make_seeding(tournament, seed_code='other')
        )

    assert (
        exc_info.value.orig.diag.constraint_name
        == 'uq_lan_tournament_seedings_tournament_target'
    )


@pytest.mark.parametrize('target', ['ffa:SE:1', 'ffa:WB:12', 'ffa:LB:9999'])
def test_target_check_accepts_ffa_round_targets(tournament, target):
    tournament_seeding_repository.create_seeding(
        _make_seeding(tournament, target=target)
    )

    assert (
        tournament_seeding_repository.find_seeding(tournament.id, target)
        is not None
    )


# fmt: off
@pytest.mark.parametrize('target', [
    'other', 'ffa:GF:1', 'ffa:SE:', 'ffa:SE:12345', 'ffa:SE:1x', 'Initial',
])
# fmt: on
def test_target_check_rejects_unknown_targets(tournament, target):
    with pytest.raises(IntegrityError) as exc_info:
        tournament_seeding_repository.create_seeding(
            _make_seeding(tournament, target=target)
        )

    assert (
        exc_info.value.orig.diag.constraint_name
        == 'ck_lan_tournament_seedings_target'
    )


def test_version_check_rejects_zero(tournament):
    with pytest.raises(IntegrityError) as exc_info:
        tournament_seeding_repository.create_seeding(
            _make_seeding(tournament, version=0)
        )

    assert (
        exc_info.value.orig.diag.constraint_name
        == 'ck_lan_tournament_seedings_version'
    )


def test_set_generated_records_code_and_time(tournament):
    seeding = _make_seeding(tournament)
    tournament_seeding_repository.create_seeding(seeding)
    now = datetime.now(UTC).replace(tzinfo=None)

    tournament_seeding_repository.set_generated(
        seeding.id,
        expected_version=1,
        generated_seed_code='a b c',
        roster_snapshot=SNAPSHOT,
        now=now,
    ).unwrap()

    stored = tournament_seeding_repository.find_seeding(
        tournament.id, 'initial'
    )
    assert stored.generated_seed_code == 'a b c'
    assert stored.generated_at == now
    assert stored.version == 2


def test_set_generated_consumes_the_version(tournament):
    seeding = _make_seeding(tournament)
    tournament_seeding_repository.create_seeding(seeding)
    now = datetime.now(UTC).replace(tzinfo=None)

    result = tournament_seeding_repository.set_generated(
        seeding.id,
        expected_version=1,
        generated_seed_code='a b c',
        roster_snapshot=SNAPSHOT,
        now=now,
    )

    assert result.is_ok()
    assert result.unwrap().version == 2
    again = tournament_seeding_repository.set_generated(
        seeding.id,
        expected_version=1,
        generated_seed_code='a b c',
        roster_snapshot=SNAPSHOT,
        now=now,
    )
    assert again.unwrap_err() == STALE_ERR


def test_delete_seedings_for_tournament_removes_all(tournament):
    tournament_seeding_repository.create_seeding(_make_seeding(tournament))
    tournament_seeding_repository.create_seeding(
        _make_seeding(tournament, target='ffa:SE:1')
    )

    tournament_seeding_repository.delete_seedings_for_tournament(tournament.id)

    assert (
        tournament_seeding_repository.get_seedings_for_tournament(
            tournament.id
        )
        == []
    )


def test_roster_snapshot_written_on_every_draft_write(tournament, orga):
    first = (RosterEntry('x', 'Xray'),)
    second = (RosterEntry('y', 'Yankee'), RosterEntry('z', 'Zulu'))
    third = (RosterEntry('z', 'Zulu'),)
    seeding = _make_seeding(tournament, roster_snapshot=first)
    now = datetime.now(UTC).replace(tzinfo=None)

    def stored():
        db.session.commit()
        return tournament_seeding_repository.find_seeding(
            tournament.id, 'initial'
        ).roster_snapshot

    tournament_seeding_repository.create_seeding(seeding)
    assert stored() == first

    tournament_seeding_repository.update_seeding_code(
        seeding.id,
        seed_code='next',
        expected_version=1,
        roster_snapshot=second,
        updated_by=orga.id,
        now=now,
    ).unwrap()
    assert stored() == second

    tournament_seeding_repository.set_generated(
        seeding.id,
        expected_version=2,
        generated_seed_code='next',
        roster_snapshot=third,
        now=now,
    ).unwrap()
    assert stored() == third


def test_roster_snapshot_defaults_to_empty(tournament):
    tournament_seeding_repository.create_seeding(_make_seeding(tournament))

    found = tournament_seeding_repository.find_seeding(
        tournament.id, 'initial'
    )

    assert found.roster_snapshot == ()


def test_joined_late_flag_round_trips_on_every_draft_write(tournament, orga):
    first = (RosterEntry('x', 'Xray', True), RosterEntry('y', 'Yankee'))
    second = (RosterEntry('x', 'Xray'), RosterEntry('y', 'Yankee', True))
    third = (RosterEntry('y', 'Yankee'),)
    seeding = _make_seeding(tournament, roster_snapshot=first)
    now = datetime.now(UTC).replace(tzinfo=None)

    def stored():
        db.session.commit()
        return tournament_seeding_repository.find_seeding(
            tournament.id, 'initial'
        ).roster_snapshot

    tournament_seeding_repository.create_seeding(seeding)
    assert stored() == first

    tournament_seeding_repository.update_seeding_code(
        seeding.id,
        seed_code='next',
        expected_version=1,
        roster_snapshot=second,
        updated_by=orga.id,
        now=now,
    ).unwrap()
    assert stored() == second

    tournament_seeding_repository.set_generated(
        seeding.id,
        expected_version=2,
        generated_seed_code='next',
        roster_snapshot=third,
        now=now,
    ).unwrap()
    assert stored() == third


def test_snapshot_without_joined_late_key_reads_as_not_new(tournament):
    tournament_seeding_repository.create_seeding(_make_seeding(tournament))
    row = db.session.execute(
        db.select(DbTournamentSeeding).filter_by(tournament_id=tournament.id)
    ).scalar_one()
    row.roster_snapshot = [{'id': 'a', 'label': 'Alpha'}]
    db.session.commit()

    found = tournament_seeding_repository.find_seeding(
        tournament.id, 'initial'
    )

    assert found.roster_snapshot == (RosterEntry('a', 'Alpha'),)
    assert found.roster_snapshot[0].joined_late is False
