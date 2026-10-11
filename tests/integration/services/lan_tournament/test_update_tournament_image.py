"""
tests.integration.services.lan_tournament.test_update_tournament_image
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

`update_tournament` takes an uploaded image and its alt text, and keeps
its legacy image behaviour when neither is passed.
"""

from datetime import datetime, UTC
from unittest.mock import patch
from uuid import uuid4

import pytest

from byceps.database import db
from byceps.services.lan_tournament import (
    tournament_image_repository,
    tournament_image_service,
    tournament_repository,
    tournament_service,
)
from byceps.services.lan_tournament.models import ContestantType
from byceps.services.lan_tournament.models.tournament_image import (
    TournamentImage,
    TournamentImageID,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.party.models import PartyID
from byceps.util.image.image_type import ImageType
from byceps.util.uuid import generate_uuid7


PARTY_ID = PartyID('lan-party-update-tournament-image')
SIBLING_PARTY_ID = PartyID('lan-party-update-tournament-image-sibling')
FOREIGN_PARTY_ID = PartyID('lan-party-update-tournament-image-foreign')

UNAVAILABLE = tournament_service.IMAGE_UNAVAILABLE_ERROR


@pytest.fixture(scope='module')
def brand(make_brand):
    return make_brand('updateimagebrand', 'Update Tournament Image Brand')


@pytest.fixture(scope='module')
def party(make_party, brand):
    return make_party(brand, PARTY_ID, 'Update Tournament Image Party')


@pytest.fixture(scope='module')
def sibling_party(make_party, brand):
    return make_party(brand, SIBLING_PARTY_ID, 'Update Image Sibling Party')


@pytest.fixture(scope='module')
def foreign_party(make_party, make_brand):
    other_brand = make_brand(
        'updateimageotherbrand', 'Update Tournament Image Other Brand'
    )
    return make_party(
        other_brand, FOREIGN_PARTY_ID, 'Update Image Foreign Party'
    )


@pytest.fixture(scope='module')
def creator(make_user):
    return make_user('UpdateTournamentImageCreator')


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


def _create(*, status=None, **kwargs):
    tournament, _event = tournament_service.create_tournament(
        PARTY_ID,
        f'Update Image Cup {uuid4().hex[:8]}',
        contestant_type=ContestantType.SOLO,
        tournament_status=status,
        **kwargs,
    ).unwrap()
    return tournament


def _update(tournament, **overrides):
    kwargs = dict(
        name=tournament.name,
        contestant_type=tournament.contestant_type,
        image_url=tournament.image_url,
        points_carry_to_losers=tournament.points_carry_to_losers,
    )
    kwargs.update(overrides)
    return tournament_service.update_tournament(tournament.id, **kwargs)


def _stored(tournament):
    db.session.expire_all()
    return tournament_repository.get_tournament(tournament.id)


def test_update_attaches_an_uploaded_image_and_derives_the_url(
    party, make_image
):
    tournament = _create()
    image = make_image()

    result = _update(
        tournament,
        image_id=image.id,
        image_url='https://evil.example/posted.png',
        image_alt_text='A trophy',
    )

    assert result.is_ok()
    expected_url = tournament_image_service.get_image_url_path(image)
    stored = _stored(tournament)
    assert stored.image_id == image.id
    assert stored.image_url == expected_url
    assert stored.image_alt_text == 'A trophy'


def test_update_attaches_a_brand_image_from_another_party(
    party, sibling_party, make_image
):
    tournament = _create()
    image = make_image(SIBLING_PARTY_ID)

    result = _update(tournament, image_id=image.id)

    assert result.is_ok()
    stored = _stored(tournament)
    assert stored.image_id == image.id
    assert stored.image_url == tournament_image_service.get_image_url_path(
        image
    )


def test_update_refuses_an_image_of_another_brand(
    party, foreign_party, make_image
):
    image = make_image(FOREIGN_PARTY_ID)
    tournament = _create(image_url='https://example.com/old.png')

    result = _update(
        tournament,
        name='Renamed By A Refused Update',
        image_id=image.id,
        image_url='https://example.com/new.png',
        image_alt_text='Not stored',
    )

    assert result.is_err()
    assert result.unwrap_err() == UNAVAILABLE
    stored = _stored(tournament)
    assert stored.name == tournament.name
    assert stored.image_id is None
    assert stored.image_url == 'https://example.com/old.png'
    assert stored.image_alt_text is None


def test_update_refuses_an_unknown_image_id(party):
    tournament = _create()

    result = _update(tournament, image_id=TournamentImageID(generate_uuid7()))

    assert result.is_err()
    assert result.unwrap_err() == UNAVAILABLE
    assert _stored(tournament).image_id is None


def test_update_maps_an_image_fk_violation_to_unavailable(party, make_image):
    tournament = _create()
    image = make_image()
    tournament_image_repository.delete_image(image.id)
    db.session.commit()

    # The image vanishes between the attachability check and the write.
    with patch.object(
        tournament_image_service,
        'find_attachable_image',
        return_value=image,
    ):
        result = _update(tournament, name='Never Stored', image_id=image.id)

    assert result.is_err()
    assert result.unwrap_err() == UNAVAILABLE
    stored = _stored(tournament)
    assert stored.name == tournament.name
    assert stored.image_id is None


def test_update_keeps_the_attached_image_the_party_may_no_longer_pick(
    party, make_image
):
    image = make_image()
    served_url = tournament_image_service.get_image_url_path(image)
    tournament = _create(image_id=image.id, image_alt_text='A trophy')

    with patch.object(
        tournament_image_service, 'find_attachable_image', return_value=None
    ):
        result = _update(
            tournament,
            name='Renamed With A Stale Image',
            image_id=image.id,
            image_alt_text='A trophy',
        )

    assert result.is_ok()
    stored = _stored(tournament)
    assert stored.name == 'Renamed With A Stale Image'
    assert stored.image_id == image.id
    assert stored.image_url == served_url
    assert stored.image_alt_text == 'A trophy'


def test_update_with_image_id_none_clears_the_image_and_keeps_the_posted_url(
    party, make_image
):
    image = make_image()
    served_url = tournament_image_service.get_image_url_path(image)
    kept = _create(image_id=image.id, image_alt_text='A trophy')
    assert kept.image_id == image.id

    # Unchanged URL: only the explicit `None` drops the image id.
    result = _update(kept, image_id=None, image_alt_text=None)

    assert result.is_ok()
    stored = _stored(kept)
    assert stored.image_id is None
    assert stored.image_url == served_url
    assert stored.image_alt_text is None

    replaced = _create(image_id=image.id, image_alt_text='A trophy')

    result = _update(
        replaced,
        image_id=None,
        image_url='https://example.com/posted.png',
        image_alt_text=None,
    )

    assert result.is_ok()
    stored = _stored(replaced)
    assert stored.image_id is None
    assert stored.image_url == 'https://example.com/posted.png'
    assert stored.image_alt_text is None


def test_update_with_image_id_none_validates_a_changed_url(party, make_image):
    image = make_image()
    tournament = _create(image_id=image.id)

    result = _update(tournament, image_id=None, image_url='javascript:alert(1)')

    assert result.is_err()
    assert _stored(tournament).image_id == image.id


def test_update_without_image_kwargs_keeps_the_legacy_behaviour(
    party, make_image
):
    image = make_image()
    tournament = _create(image_id=image.id, image_alt_text='A trophy')

    unchanged = _update(tournament, description='Edited')

    assert unchanged.is_ok()
    stored = _stored(tournament)
    assert stored.image_id == image.id
    assert stored.image_url == tournament.image_url
    assert stored.image_alt_text == 'A trophy'

    changed = _update(tournament, image_url='https://example.com/other.png')

    assert changed.is_ok()
    stored = _stored(tournament)
    assert stored.image_id is None
    assert stored.image_url == 'https://example.com/other.png'
    assert stored.image_alt_text is None


def test_update_replacing_the_image_without_alt_text_drops_the_old_alt_text(
    party, make_image
):
    first = make_image()
    second = make_image()
    tournament = _create(image_id=first.id, image_alt_text='First image')

    same = _update(tournament, image_id=first.id)

    assert same.is_ok()
    assert _stored(tournament).image_alt_text == 'First image'

    replaced = _update(tournament, image_id=second.id)

    assert replaced.is_ok()
    stored = _stored(tournament)
    assert stored.image_id == second.id
    assert stored.image_alt_text is None


def test_update_stores_alt_text_only_with_an_image(party, make_image):
    without_image = _create()

    result = _update(without_image, image_alt_text='Orphaned')

    assert result.is_ok()
    assert _stored(without_image).image_alt_text is None

    with_url = _create(image_url='https://example.com/banner.png')

    result = _update(with_url, image_alt_text='A banner')

    assert result.is_ok()
    assert _stored(with_url).image_alt_text == 'A banner'

    result = _update(with_url, image_alt_text='')

    assert result.is_ok()
    assert _stored(with_url).image_alt_text is None

    image = make_image()
    result = _update(without_image, image_id=image.id, image_alt_text='Shown')

    assert result.is_ok()
    assert _stored(without_image).image_alt_text == 'Shown'


def test_update_changes_the_image_of_an_ongoing_tournament(party, make_image):
    old = make_image()
    new = make_image()
    tournament = _create(
        status=TournamentStatus.ONGOING,
        image_id=old.id,
        image_alt_text='Old',
    )
    assert tournament.tournament_status is TournamentStatus.ONGOING

    result = _update(tournament, image_id=new.id, image_alt_text='New')

    assert result.is_ok()
    stored = _stored(tournament)
    assert stored.image_id == new.id
    assert stored.image_url == tournament_image_service.get_image_url_path(new)
    assert stored.image_alt_text == 'New'
    assert stored.tournament_status is TournamentStatus.ONGOING
