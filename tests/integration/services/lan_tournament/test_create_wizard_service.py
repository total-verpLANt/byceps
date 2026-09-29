"""
tests.integration.services.lan_tournament.test_create_wizard_service
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from datetime import datetime, UTC
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from byceps.database import db
from byceps.services.lan_tournament import (
    tournament_image_repository,
    tournament_image_service,
    tournament_repository,
    tournament_request_service,
    tournament_service,
)
from byceps.services.lan_tournament.dbmodels.tournament import DbTournament
from byceps.services.lan_tournament.models import ContestantType
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.tournament_image import (
    TournamentImage,
    TournamentImageID,
)
from byceps.services.party.models import PartyID
from byceps.util.image.image_type import ImageType
from byceps.util.uuid import generate_uuid7


PARTY_ID = PartyID('lan-party-create-wizard-service')
SIBLING_PARTY_ID = PartyID('lan-party-create-wizard-service-sibling')
FOREIGN_PARTY_ID = PartyID('lan-party-create-wizard-service-foreign')


@pytest.fixture(scope='module')
def party(make_party, brand):
    return make_party(brand, PARTY_ID, 'Create Wizard Service Party')


@pytest.fixture(scope='module')
def sibling_party(make_party, brand):
    return make_party(brand, SIBLING_PARTY_ID, 'Create Wizard Sibling')


@pytest.fixture(scope='module')
def foreign_party(make_party, make_brand):
    other_brand = make_brand(
        'create-wizard-other-brand', 'Create Wizard Other Brand'
    )
    return make_party(other_brand, FOREIGN_PARTY_ID, 'Create Wizard Foreign')


@pytest.fixture(scope='module')
def creator(make_user):
    return make_user('CreateWizardServiceCreator')


@pytest.fixture(scope='module')
def decider(make_user):
    return make_user('CreateWizardServiceDecider')


@pytest.fixture
def make_image(creator):
    def _wrapper(party_id: PartyID = PARTY_ID) -> TournamentImage:
        image = TournamentImage(
            id=TournamentImageID(generate_uuid7()),
            party_id=party_id,
            creator_id=creator.id,
            created_at=datetime.now(UTC),
            filename='seed.png',
            image_type=ImageType.png,
            width=1920,
            height=1080,
            byte_size=10,
        )
        tournament_image_repository.create_image(image)
        db.session.commit()
        return image

    return _wrapper


def _create(party_id=PARTY_ID, name='Wizard Cup', **kwargs):
    return tournament_service.create_tournament(
        party_id, name, contestant_type=ContestantType.SOLO, **kwargs
    )


def _update(tournament, **overrides):
    kwargs = dict(
        name=tournament.name,
        contestant_type=tournament.contestant_type,
        image_url=tournament.image_url,
    )
    kwargs.update(overrides)
    return tournament_service.update_tournament(tournament.id, **kwargs)


def _count_by_token(token) -> int:
    return db.session.scalar(
        select(func.count())
        .select_from(DbTournament)
        .where(DbTournament.creation_token == token)
    )


def test_create_with_image_id_sets_served_image_url(party, make_image):
    image = make_image()

    result = _create(
        image_id=image.id,
        image_alt_text='A trophy',
        image_url='https://evil.example/posted.png',
    )

    assert result.is_ok()
    tournament, _event = result.unwrap()
    expected_url = tournament_image_service.get_image_url_path(image)
    assert tournament.image_url == expected_url
    assert tournament.image_id == image.id
    assert tournament.image_alt_text == 'A trophy'

    stored = tournament_repository.get_tournament(tournament.id)
    assert stored.image_url == expected_url
    assert stored.image_id == image.id
    assert stored.image_alt_text == 'A trophy'


def test_create_with_sibling_party_image_is_accepted(
    party, sibling_party, make_image
):
    image = make_image(SIBLING_PARTY_ID)

    result = _create(image_id=image.id)

    assert result.is_ok()


def test_create_with_foreign_brand_image_is_rejected(
    party, foreign_party, make_image
):
    image = make_image(FOREIGN_PARTY_ID)
    name = f'Foreign Image Cup {uuid4()}'

    result = _create(name=name, image_id=image.id)

    assert result.is_err()
    assert result.unwrap_err() == tournament_service.IMAGE_UNAVAILABLE_ERROR
    assert (
        db.session.scalar(
            select(func.count())
            .select_from(DbTournament)
            .where(DbTournament.name == name)
        )
        == 0
    )


def test_create_with_unknown_image_id_is_rejected(party):
    result = _create(image_id=TournamentImageID(generate_uuid7()))

    assert result.is_err()
    assert result.unwrap_err() == tournament_service.IMAGE_UNAVAILABLE_ERROR


def test_create_same_creation_token_twice_returns_duplicate_err_one_row(
    party,
):
    token = uuid4()

    first = _create(name='Token Cup 1', creation_token=token)
    second = _create(name='Token Cup 2', creation_token=token)

    assert first.is_ok()
    assert second.is_err()
    assert second.unwrap_err() == tournament_service.DUPLICATE_SUBMISSION_ERROR
    assert _count_by_token(token) == 1

    found = tournament_service.find_tournament_by_creation_token(token)
    assert found is not None
    assert found.id == first.unwrap()[0].id
    assert found.party_id == PARTY_ID


def test_create_maps_image_fk_violation_to_unavailable(party, make_image):
    image = make_image()
    tournament_image_repository.delete_image(image.id)
    db.session.commit()
    name = f'Purged Image Cup {uuid4()}'

    # The image vanishes between the attachability check and the insert.
    with patch.object(
        tournament_image_service,
        'find_attachable_image',
        return_value=image,
    ):
        result = _create(name=name, image_id=image.id)

    assert result.is_err()
    assert result.unwrap_err() == tournament_service.IMAGE_UNAVAILABLE_ERROR
    assert (
        db.session.scalar(
            select(func.count())
            .select_from(DbTournament)
            .where(DbTournament.name == name)
        )
        == 0
    )


def test_create_from_request_maps_image_fk_violation_to_unavailable(
    party, make_image, creator, decider
):
    submit_result = tournament_request_service.submit_request(
        party.id,
        creator.id,
        party_capacity=None,
        name='FK Race Request Cup',
        game='Rocket League',
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        team_size=1,
        participant_limit=8,
        preferred_start_time=datetime(2026, 10, 24, 18, 0, tzinfo=UTC),
        preferred_end_time=datetime(2026, 10, 24, 22, 0, tzinfo=UTC),
        description='A friendly cup.',
    )
    request, _event = submit_result.unwrap()
    assert tournament_request_service.accept_request(
        request.id, decider.id
    ).is_ok()

    image = make_image()
    tournament_image_repository.delete_image(image.id)
    db.session.commit()
    name = f'Purged Request Image Cup {uuid4()}'

    with patch.object(
        tournament_image_service,
        'find_attachable_image',
        return_value=image,
    ):
        result = _create(
            name=name,
            image_id=image.id,
            created_from_request_id=request.id,
            initiator_id=decider.id,
        )

    assert result.is_err()
    assert result.unwrap_err() == tournament_service.IMAGE_UNAVAILABLE_ERROR
    assert (
        db.session.scalar(
            select(func.count())
            .select_from(DbTournament)
            .where(DbTournament.name == name)
        )
        == 0
    )


def test_update_clears_image_id_and_alt_when_image_url_changes(
    party, make_image
):
    image = make_image()
    tournament, _event = _create(
        image_id=image.id, image_alt_text='A trophy'
    ).unwrap()

    result = _update(tournament, image_url='https://cdn.example/new.png')

    assert result.is_ok()
    stored = tournament_repository.get_tournament(tournament.id)
    assert stored.image_url == 'https://cdn.example/new.png'
    assert stored.image_id is None
    assert stored.image_alt_text is None


def test_update_keeps_image_when_image_url_unchanged(party, make_image):
    image = make_image()
    tournament, _event = _create(
        image_id=image.id, image_alt_text='A trophy'
    ).unwrap()

    result = _update(tournament, description='Now with a description')

    assert result.is_ok()
    stored = tournament_repository.get_tournament(tournament.id)
    assert stored.description == 'Now with a description'
    assert stored.image_url == tournament.image_url
    assert stored.image_id == image.id
    assert stored.image_alt_text == 'A trophy'


def test_update_accepts_unchanged_relative_served_url(party, make_image):
    image = make_image()
    tournament, _event = _create(image_id=image.id).unwrap()
    assert tournament.image_url.startswith('/data/parties/')

    result = _update(tournament, name='Renamed Wizard Cup')

    assert result.is_ok()
    assert (
        tournament_repository.get_tournament(tournament.id).name
        == 'Renamed Wizard Cup'
    )


def test_update_rejects_changed_relative_url(party, make_image):
    image = make_image()
    tournament, _event = _create(image_id=image.id).unwrap()

    result = _update(tournament, image_url='/data/other.png')

    assert result.is_err()
    stored = tournament_repository.get_tournament(tournament.id)
    assert stored.image_id == image.id
