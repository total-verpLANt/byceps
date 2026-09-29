"""
tests.integration.services.lan_tournament.test_tournament_image_repository
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

import dataclasses
from datetime import datetime, timedelta, UTC
from uuid import UUID

import pytest

from byceps.database import db
from byceps.services.lan_tournament import (
    tournament_domain_service,
    tournament_image_repository,
    tournament_repository,
)
from byceps.services.lan_tournament.models.tournament_image import (
    TournamentImage,
    TournamentImageID,
)
from byceps.services.party.models import PartyID
from byceps.util.image.image_type import ImageType
from byceps.util.uuid import generate_uuid4, generate_uuid7


PARTY_ID = PartyID('lan-party-image-repository')
OTHER_PARTY_ID = PartyID('lan-party-image-repository-other')

NOW = datetime.now(UTC)


@pytest.fixture(scope='module')
def party(make_party, brand):
    return make_party(brand, PARTY_ID, 'Image Repository Party')


@pytest.fixture(scope='module')
def other_party(make_party, brand):
    return make_party(brand, OTHER_PARTY_ID, 'Image Repository Other Party')


@pytest.fixture(scope='module')
def creator(make_user):
    return make_user('ImageRepositoryCreator')


@pytest.fixture
def make_image(party, other_party, creator):
    def _wrapper(
        *,
        party_id: PartyID = PARTY_ID,
        filename: str = 'banner.png',
        created_at: datetime | None = None,
    ) -> TournamentImage:
        image = TournamentImage(
            id=TournamentImageID(generate_uuid7()),
            party_id=party_id,
            creator_id=creator.id,
            created_at=created_at if created_at is not None else NOW,
            filename=filename,
            image_type=ImageType.png,
            width=1920,
            height=1080,
            byte_size=123_456,
        )
        tournament_image_repository.create_image(image)
        db.session.commit()
        return image

    return _wrapper


@pytest.fixture
def make_tournament(party):
    def _wrapper(name: str, **kwargs):
        tournament, _event = tournament_domain_service.create_tournament(
            PARTY_ID, name, **kwargs
        )
        tournament_repository.create_tournament(tournament)
        return tournament

    return _wrapper


def test_create_and_find_image_roundtrip(make_image):
    image = make_image(filename='Rundum Sorglos.png')

    found = tournament_image_repository.find_image(image.id)

    assert found == image
    assert found.image_type is ImageType.png


def test_find_image_returns_none_for_unknown_id():
    unknown = TournamentImageID(generate_uuid7())

    assert tournament_image_repository.find_image(unknown) is None


def test_delete_image_removes_the_row(make_image):
    image = make_image()

    tournament_image_repository.delete_image(image.id)
    db.session.commit()

    assert tournament_image_repository.find_image(image.id) is None


def test_is_image_referenced_after_tournament_links_it(
    make_image, make_tournament
):
    image = make_image()
    assert not tournament_image_repository.is_image_referenced(image.id)

    make_tournament('Referencing Cup', image_id=image.id)

    assert tournament_image_repository.is_image_referenced(image.id)


def test_find_unused_images_skips_referenced_recent_and_other_party(
    make_image, make_tournament
):
    cutoff = NOW - timedelta(hours=24)
    old = NOW - timedelta(hours=48)
    older = NOW - timedelta(hours=72)

    unused_old = make_image(filename='unused-old.png', created_at=old)
    unused_older = make_image(filename='unused-older.png', created_at=older)
    referenced = make_image(filename='keep-referenced.png', created_at=old)
    make_tournament('Keeps The Image', image_id=referenced.id)
    make_image(filename='keep-recent.png', created_at=NOW)
    make_image(
        party_id=OTHER_PARTY_ID, filename='keep-other.png', created_at=older
    )

    unused = tournament_image_repository.find_unused_images(
        PARTY_ID, created_before=cutoff, limit=100
    )
    ids = [image.id for image in unused]

    assert referenced.id not in ids
    assert {i.filename for i in unused}.isdisjoint(
        {'keep-referenced.png', 'keep-recent.png', 'keep-other.png'}
    )
    assert ids.index(unused_older.id) < ids.index(unused_old.id)

    limited = tournament_image_repository.find_unused_images(
        PARTY_ID, created_before=cutoff, limit=1
    )
    assert [image.id for image in limited] == [ids[0]]


def test_summarize_unused_images_sums_byte_size(make_image, make_tournament):
    cutoff = NOW - timedelta(hours=24)
    old = NOW - timedelta(hours=48)
    summarize = tournament_image_repository.summarize_unused_images
    count_before, size_before = summarize(PARTY_ID, created_before=cutoff)

    make_image(filename='sum-a.png', created_at=old)
    make_image(filename='sum-b.png', created_at=old)
    referenced = make_image(filename='sum-referenced.png', created_at=old)
    make_tournament('Sum Keeps The Image', image_id=referenced.id)
    make_image(filename='sum-recent.png', created_at=NOW)
    make_image(
        party_id=OTHER_PARTY_ID, filename='sum-other.png', created_at=old
    )

    count, byte_size = summarize(PARTY_ID, created_before=cutoff)

    assert count - count_before == 2
    assert byte_size - size_before == 2 * 123_456


def test_lock_images_for_update_returns_existing_rows_in_id_order(make_image):
    images = [make_image(filename=f'lock-{n}.png') for n in range(3)]
    unknown = TournamentImageID(generate_uuid7())
    requested = [images[2].id, unknown, images[0].id, images[1].id]

    locked = tournament_image_repository.lock_images_for_update(requested)

    assert [image.id for image in locked] == sorted(
        image.id for image in images
    )
    assert unknown not in {image.id for image in locked}


def test_list_images_filters_parties_and_escapes_like_wildcards(make_image):
    make_image(filename='100% legit.png')
    make_image(filename='a_b.png')
    make_image(filename='axb.png')
    make_image(filename='c\\d.png')
    make_image(party_id=OTHER_PARTY_ID, filename='100% other.png')

    def names(query, party_ids=(PARTY_ID,)):
        items = tournament_image_repository.list_images(
            party_ids, filename_query=query, limit=100, offset=0
        )
        return {item.image.filename for item in items}

    assert names('%') == {'100% legit.png'}
    assert names('_') == {'a_b.png'}
    assert names('\\') == {'c\\d.png'}
    assert {'a_b.png', 'axb.png'} <= names('B.PNG')
    assert 'axb.png' not in names('_')
    assert names('%', (PARTY_ID, OTHER_PARTY_ID)) == {
        '100% legit.png',
        '100% other.png',
    }
    assert names('%', (OTHER_PARTY_ID,)) == {'100% other.png'}
    assert names('%', ()) == set()

    assert (
        tournament_image_repository.count_images(
            [PARTY_ID, OTHER_PARTY_ID], filename_query='%'
        )
        == 2
    )
    assert (
        tournament_image_repository.count_images([PARTY_ID], filename_query='%')
        == 1
    )


def test_list_images_newest_first_with_used_by_names(
    make_image, make_tournament
):
    older = make_image(
        filename='order-older.png', created_at=NOW - timedelta(minutes=5)
    )
    newer = make_image(filename='order-newer.png', created_at=NOW)
    make_tournament('Zeta Used By', image_id=older.id)
    make_tournament('Alpha Used By', image_id=older.id)

    items = tournament_image_repository.list_images(
        [PARTY_ID], filename_query='order-', limit=10, offset=0
    )

    assert [item.image.id for item in items] == [newer.id, older.id]
    assert items[0].used_by == ()
    assert items[1].used_by == ('Alpha Used By', 'Zeta Used By')

    page = tournament_image_repository.list_images(
        [PARTY_ID], filename_query='order-', limit=1, offset=1
    )
    assert [item.image.id for item in page] == [older.id]


def test_find_tournament_by_creation_token(make_tournament):
    token = generate_uuid4()
    created = make_tournament('Token Cup', creation_token=token)

    found = tournament_repository.find_tournament_by_creation_token(token)

    assert found is not None
    assert found.id == created.id
    assert (
        tournament_repository.find_tournament_by_creation_token(
            generate_uuid4()
        )
        is None
    )


def test_tournament_roundtrip_maps_image_alt_text_and_token(
    make_image, make_tournament
):
    image = make_image()
    token = generate_uuid4()
    created = make_tournament(
        'Roundtrip Cup',
        image_id=image.id,
        image_alt_text='A banner',
        creation_token=token,
    )

    loaded = tournament_repository.get_tournament(created.id)

    assert loaded.image_id == image.id
    assert isinstance(loaded.image_id, UUID)
    assert loaded.image_alt_text == 'A banner'
    assert loaded.creation_token == token


def test_update_tournament_persists_image_fields_but_never_the_token(
    make_image, make_tournament
):
    image = make_image()
    token = generate_uuid4()
    created = make_tournament('Update Cup', creation_token=token)

    tournament_repository.update_tournament(
        dataclasses.replace(
            created,
            image_id=image.id,
            image_alt_text='Now with cover',
            creation_token=generate_uuid4(),
        )
    )

    loaded = tournament_repository.get_tournament(created.id)
    assert loaded.image_id == image.id
    assert loaded.image_alt_text == 'Now with cover'
    assert loaded.creation_token == token
