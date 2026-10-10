"""
tests.integration.services.lan_tournament.test_tournament_config_service
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

import dataclasses
from datetime import datetime, UTC
from hashlib import sha256
import json
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import column, select, table, text

from byceps.database import db
from byceps.services.lan_tournament import (
    tournament_config_document,
    tournament_config_service,
    tournament_image_repository,
    tournament_image_service,
    tournament_repository,
    tournament_request_service,
    tournament_service,
)
from byceps.services.lan_tournament.models import ContestantType
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.playoff import PlayoffReleaseMode
from byceps.services.lan_tournament.models.score_ordering import ScoreOrdering
from byceps.services.lan_tournament.models.tournament import Tournament
from byceps.services.lan_tournament.models.tournament_category import (
    TournamentCategory,
)
from byceps.services.lan_tournament.models.tournament_image import (
    TournamentImage,
    TournamentImageID,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.lan_tournament.tournament_config_domain_service import (
    config_input_of,
    TournamentConfigInput,
)
from byceps.services.party.models import PartyID
from byceps.util.image.image_type import ImageType
from byceps.util.result import Err
from byceps.util.uuid import generate_uuid7


PARTY_A_ID = PartyID('lan-party-config-service-a')
PARTY_B_ID = PartyID('lan-party-config-service-b')
PARTY_POSITIONS_ID = PartyID('lan-party-config-service-positions')


@pytest.fixture(scope='module')
def party_a(make_party, brand):
    return make_party(brand, PARTY_A_ID, 'Config Service Party A')


@pytest.fixture(scope='module')
def party_b(make_party, brand):
    return make_party(brand, PARTY_B_ID, 'Config Service Party B')


@pytest.fixture(scope='module')
def party_positions(make_party, brand):
    return make_party(brand, PARTY_POSITIONS_ID, 'Config Service Positions')


@pytest.fixture(scope='module')
def admin(make_user):
    return make_user('ConfigServiceAdmin')


@pytest.fixture(scope='module')
def decider(make_user):
    return make_user('ConfigServiceDecider')


@pytest.fixture
def make_image(admin):
    def _wrapper(party_id: PartyID) -> TournamentImage:
        image = TournamentImage(
            id=TournamentImageID(generate_uuid7()),
            party_id=party_id,
            creator_id=admin.id,
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


# -- reads: only committed data, through a separate connection --


def _committed(sql: str, **params):
    with db.engine.connect() as connection:
        return connection.execute(text(sql), params).all()


def _counts() -> tuple[int, int]:
    return (
        _committed('SELECT count(*) FROM lan_tournaments')[0][0],
        _committed('SELECT count(*) FROM lan_tournament_log_entries')[0][0],
    )


def _committed_log_entries(tournament_id) -> list[tuple]:
    return _committed(
        'SELECT event_type, initiator_id, data'
        ' FROM lan_tournament_log_entries'
        ' WHERE tournament_id = :id',
        id=tournament_id,
    )


def _committed_columns(tournament_id, *columns: str) -> tuple:
    statement = (
        select(*[column(name) for name in columns])
        .select_from(table('lan_tournaments'))
        .where(column('id') == tournament_id)
    )
    with db.engine.connect() as connection:
        rows = connection.execute(statement).all()
    assert len(rows) == 1
    return tuple(rows[0])


# -- helpers --


def _create(party, name, initiator, **kwargs) -> Tournament:
    result = tournament_service.create_tournament(
        party.id, name, initiator_id=initiator.id, **kwargs
    )
    tournament, _event = result.unwrap()
    return tournament


def _export(tournament, initiator) -> bytes:
    return tournament_config_service.export_tournament_config(
        tournament, initiator.id
    )


def _import(party, raw: bytes, initiator, **kwargs) -> Tournament:
    check = tournament_config_service.check_import(raw).unwrap()
    result = tournament_config_service.import_tournament_config(
        party.id, check, initiator.id, **kwargs
    )
    tournament, _event = result.unwrap()
    return tournament


def _stored_input(tournament) -> TournamentConfigInput:
    return config_input_of(tournament_service.get_tournament(tournament.id))


SOLO_SE_INPUT = TournamentConfigInput(
    name='Import Cup',
    category='MAIN',
    contestant_type='SOLO',
    game_format='ONE_V_ONE',
    elimination_mode='SINGLE_ELIMINATION',
    min_players=4,
    max_players=16,
)
HIGHSCORE_FFA_PLAYOFFS_INPUT = TournamentConfigInput(
    name='Hostile Cup',
    category='MAIN',
    contestant_type='SOLO',
    game_format='HIGHSCORE',
    score_ordering='HIGHER_IS_BETTER',
    min_players=8,
    max_players=48,
    point_table=[10, 7, 5, 3],
    group_size_min=3,
    group_size_max=4,
    advancement_count=1,
    playoff_enabled=True,
    playoff_qualifier_count=16,
    playoff_elimination_mode='SINGLE_ELIMINATION',
    playoff_release_mode='AUTOMATIC',
)


def _document(base=SOLO_SE_INPUT, /, **overrides: Any) -> bytes:
    values = dataclasses.asdict(base) | overrides
    return tournament_config_document.serialize(TournamentConfigInput(**values))


def _accepted_request(party, proposer, decider):
    request, _event = tournament_request_service.submit_request(
        party.id,
        proposer.id,
        party_capacity=None,
        name=f'Config Request Cup {uuid4()}',
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


# fmt: off
_FAMILIES = [
    pytest.param(
        {
            'category': TournamentCategory.MAIN,
            'game': 'Chess',
            'description': 'Open to all.',
            'ruleset': 'Best of three.',
            'start_time': datetime(2026, 10, 24, 18, 0),
            'contestant_type': ContestantType.SOLO,
            'game_format': GameFormat.ONE_V_ONE,
            'elimination_mode': EliminationMode.SINGLE_ELIMINATION,
            'min_players': 4,
            'max_players': 16,
        },
        id='solo_1v1_se',
    ),
    pytest.param(
        {
            'category': TournamentCategory.FUN,
            'contestant_type': ContestantType.TEAM,
            'game_format': GameFormat.ONE_V_ONE,
            'elimination_mode': EliminationMode.DOUBLE_ELIMINATION,
            'min_teams': 4,
            'max_teams': 16,
            'min_players_in_team': 2,
            'max_players_in_team': 3,
        },
        id='team_1v1_de',
    ),
    pytest.param(
        {
            'category': TournamentCategory.MAIN,
            'contestant_type': ContestantType.SOLO,
            'game_format': GameFormat.ONE_V_ONE,
            'elimination_mode': EliminationMode.ROUND_ROBIN,
            'min_players': 12,
            'max_players': 24,
            'playoff_game_format': GameFormat.ONE_V_ONE,
            'playoff_elimination_mode': EliminationMode.SINGLE_ELIMINATION,
            'playoff_group_count': 3,
            'playoff_qualifiers_per_group': 2,
            'playoff_release_mode': PlayoffReleaseMode.MANUAL,
        },
        id='round_robin_playoffs',
    ),
    pytest.param(
        {
            'category': TournamentCategory.MAIN,
            'contestant_type': ContestantType.SOLO,
            'game_format': GameFormat.FREE_FOR_ALL,
            'elimination_mode': EliminationMode.SINGLE_ELIMINATION,
            'min_players': 8,
            'max_players': 32,
            'point_table': [10, 7, 5],
            'group_size_min': 3,
            'group_size_max': 4,
            'advancement_count': 2,
        },
        id='ffa_se',
    ),
    pytest.param(
        {
            'category': TournamentCategory.MAIN,
            'contestant_type': ContestantType.SOLO,
            'game_format': GameFormat.FREE_FOR_ALL,
            'elimination_mode': EliminationMode.DOUBLE_ELIMINATION,
            'min_players': 8,
            'max_players': 32,
            'point_table': [10, 7, 5],
            'group_size_min': 3,
            'group_size_max': 4,
            'advancement_count': 2,
            'points_carry_to_losers': True,
        },
        id='ffa_de_with_carry',
    ),
    pytest.param(
        {
            'category': TournamentCategory.FUN,
            'contestant_type': ContestantType.TEAM,
            'game_format': GameFormat.FREE_FOR_ALL,
            'elimination_mode': EliminationMode.DOUBLE_ELIMINATION,
            'min_teams': 8,
            'max_teams': 24,
            'min_players_in_team': 2,
            'max_players_in_team': 3,
            'point_table': [10, 8, 6, 4, 2, 1],
            'group_size_min': 4,
            'group_size_max': 6,
            'advancement_count': 2,
            'points_carry_to_losers': True,
        },
        id='team_ffa',
    ),
    pytest.param(
        {
            'category': TournamentCategory.MAIN,
            'contestant_type': ContestantType.SOLO,
            'game_format': GameFormat.HIGHSCORE,
            'elimination_mode': EliminationMode.NONE,
            'score_ordering': ScoreOrdering.LOWER_IS_BETTER,
            'max_players': 64,
        },
        id='highscore',
    ),
    pytest.param(
        {
            'category': TournamentCategory.MAIN,
            'contestant_type': ContestantType.SOLO,
            'game_format': GameFormat.HIGHSCORE,
            'elimination_mode': EliminationMode.NONE,
            'score_ordering': ScoreOrdering.HIGHER_IS_BETTER,
            'min_players': 8,
            'max_players': 48,
            'point_table': [10, 7, 5, 3],
            'group_size_min': 3,
            'group_size_max': 4,
            'advancement_count': 2,
            'points_carry_to_losers': True,
            'playoff_game_format': GameFormat.FREE_FOR_ALL,
            'playoff_elimination_mode': EliminationMode.DOUBLE_ELIMINATION,
            'playoff_qualifier_count': 16,
            'playoff_release_mode': PlayoffReleaseMode.AUTOMATIC,
        },
        id='highscore_ffa_playoffs',
    ),
]
# fmt: on


@pytest.mark.parametrize('settings', _FAMILIES)
def test_round_trip_export_import_on_second_party(
    party_a, party_b, admin, settings
):
    source = _create(party_a, 'Round Trip Cup', admin, **settings)
    expected = _stored_input(source)
    assert expected.game_format == settings['game_format'].name
    assert expected.playoff_enabled == ('playoff_game_format' in settings)

    imported = _import(party_b, _export(source, admin), admin)

    assert imported.party_id == party_b.id
    assert imported.id != source.id
    assert _stored_input(imported) == expected


def test_repeated_export_import_is_stable(party_a, party_b, admin):
    start_time = datetime(2026, 10, 24, 18, 0, 0, 123456)
    source = _create(
        party_a,
        'Stable Cup',
        admin,
        game='',
        description=None,
        start_time=start_time,
        contestant_type=ContestantType.SOLO,
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        min_players=2,
        max_players=8,
    )

    first_document = _export(source, admin)
    first_import = _import(party_b, first_document, admin)
    second_document = _export(first_import, admin)
    second_import = _import(party_b, second_document, admin)

    assert first_document == second_document

    expected = _stored_input(source)
    assert expected.game == ''
    assert expected.description is None
    assert expected.start_time == start_time
    assert _stored_input(first_import) == expected
    assert _stored_input(second_import) == expected


def test_export_document_carries_no_instance_data(party_a, admin, decider):
    request = _accepted_request(party_a, admin, decider)
    source = _create(
        party_a,
        'Instance Data Cup',
        decider,
        image_url='https://example.test/cup.png',
        image_alt_text='A cup',
        contestant_type=ContestantType.SOLO,
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        created_from_request_id=request.id,
        creation_token=uuid4(),
    )

    raw = _export(source, decider)

    document = json.loads(raw)
    assert set(document) == {'format', 'version', 'tournament'}
    assert set(document['tournament']) == set(
        tournament_config_document.TOURNAMENT_KEYS
    )
    text_of_document = raw.decode('utf-8')
    for forbidden in (
        str(source.id),
        str(source.party_id),
        str(source.creation_token),
        str(request.id),
        'example.test',
        'image',
    ):
        assert forbidden not in text_of_document


def test_import_creates_draft_with_fresh_id_and_next_position(
    party_a, party_positions, admin
):
    for name in ('First Cup', 'Second Cup'):
        _create(
            party_positions, name, admin, contestant_type=ContestantType.SOLO
        )
    source = _create(
        party_a,
        'Open Cup',
        admin,
        tournament_status=TournamentStatus.REGISTRATION_OPEN,
        contestant_type=ContestantType.SOLO,
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
    )
    assert source.tournament_status == TournamentStatus.REGISTRATION_OPEN
    existing_positions = sorted(
        t.position
        for t in tournament_service.get_tournaments_for_party(
            party_positions.id
        )
    )
    assert existing_positions == [1, 2]

    result = tournament_config_service.import_tournament_config(
        party_positions.id,
        tournament_config_service.check_import(_export(source, admin)).unwrap(),
        admin.id,
    )

    imported, event = result.unwrap()
    assert imported.id != source.id
    assert event.tournament_id == imported.id
    assert imported.tournament_status == TournamentStatus.DRAFT
    assert imported.position == 3
    assert _committed_columns(imported.id, 'party_id', 'position') == (
        party_positions.id,
        3,
    )
    assert _committed_columns(imported.id, 'tournament_status')[0] == (
        TournamentStatus.DRAFT.name
    )


def test_import_never_sets_request_link_image_or_run_state(
    party_a, party_b, admin, decider
):
    request = _accepted_request(party_a, admin, decider)
    source = _create(
        party_a,
        'User Organized Cup',
        decider,
        image_url='https://example.test/cup.png',
        image_alt_text='A cup',
        tournament_status=TournamentStatus.REGISTRATION_OPEN,
        contestant_type=ContestantType.SOLO,
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        created_from_request_id=request.id,
        creation_token=uuid4(),
    )
    assert source.category == TournamentCategory.USER_ORGANIZED
    assert source.created_from_request_id == request.id
    assert source.image_url is not None

    imported = _import(party_b, _export(source, decider), admin)

    assert _committed_columns(
        imported.id,
        'category',
        'tournament_status',
        'created_from_request_id',
        'image_id',
        'image_url',
        'image_alt_text',
        'creation_token',
        'winner_team_id',
        'winner_participant_id',
        'playoff_auto_release_suspended',
        'playoff_released_at',
        'playoff_released_by',
        'leaderboard_closed_at',
        'operational_clock_elapsed_us',
        'operational_clock_running_since',
        'operational_clock_activated_at',
    ) == (
        TournamentCategory.USER_ORGANIZED.name,
        TournamentStatus.DRAFT.name,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        False,
        None,
        None,
        None,
        0,
        None,
        None,
    )
    assert (
        tournament_service.get_tournament(imported.id).category
        == TournamentCategory.USER_ORGANIZED
    )


def test_import_attaches_the_stored_image_and_alt_text(
    party_b, admin, make_image
):
    image = make_image(party_b.id)
    check = tournament_config_service.check_import(_document()).unwrap()

    result = tournament_config_service.import_tournament_config(
        party_b.id,
        check,
        admin.id,
        image_id=image.id,
        image_alt_text='A trophy',
    )

    imported, _event = result.unwrap()
    assert _committed_columns(
        imported.id, 'image_id', 'image_url', 'image_alt_text'
    ) == (
        image.id,
        tournament_image_service.get_image_url_path(image),
        'A trophy',
    )


def test_export_logs_entry_with_document_hash(party_a, admin):
    source = _create(
        party_a,
        'Export Log Cup',
        admin,
        contestant_type=ContestantType.SOLO,
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
    )
    assert _committed_log_entries(source.id) == []

    raw = _export(source, admin)

    assert _committed_log_entries(source.id) == [
        (
            tournament_config_service.CONFIG_EXPORTED_EVENT,
            admin.id,
            {
                'format_version': tournament_config_document.VERSION,
                'document_sha256': sha256(raw).hexdigest(),
            },
        )
    ]


def test_import_logs_entry_with_the_same_hash_as_the_export(
    party_a, party_b, admin, decider
):
    source = _create(
        party_a,
        'Hash Link Cup',
        admin,
        contestant_type=ContestantType.SOLO,
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
    )
    raw = _export(source, admin)

    imported = _import(party_b, raw, decider)

    (export_entry,) = _committed_log_entries(source.id)
    (import_entry,) = _committed_log_entries(imported.id)
    assert import_entry == (
        tournament_config_service.CONFIG_IMPORTED_EVENT,
        decider.id,
        {
            'format_version': tournament_config_document.VERSION,
            'document_sha256': sha256(raw).hexdigest(),
        },
    )
    assert import_entry[2] == export_entry[2]


def test_check_import_writes_nothing(party_b, admin):
    raw = _document()
    tournaments_before, log_entries_before = _counts()

    with (
        patch.object(
            tournament_repository,
            'commit_session',
            wraps=tournament_repository.commit_session,
        ) as commit_session,
        patch.object(
            tournament_service,
            'create_tournament',
            wraps=tournament_service.create_tournament,
        ) as create_tournament,
    ):
        result = tournament_config_service.check_import(raw)

    assert result.is_ok()
    check = result.unwrap()
    assert check.document_sha256 == sha256(raw).hexdigest()
    assert check.config.name == 'Import Cup'
    commit_session.assert_not_called()
    create_tournament.assert_not_called()
    assert _counts() == (tournaments_before, log_entries_before)


# fmt: off
_HOSTILE_INTS = {
    'negative_sizes_and_huge_qualifier_count': (
        {'group_size_min': -5, 'group_size_max': 0,
         'playoff_qualifier_count': 10**400},
        {'group_size_min', 'group_size_max', 'playoff_qualifier_count'},
    ),
    'huge_group_max_and_qualifier_count': (
        {'group_size_max': 10**400, 'playoff_qualifier_count': 10**9},
        {'group_size_max', 'playoff_qualifier_count'},
    ),
    'huge_negative_advancement_and_group_max': (
        {'advancement_count': -(2**1100), 'group_size_max': -5},
        {'advancement_count', 'group_size_max'},
    ),
    'huge_group_sizes_of_both_signs': (
        {'group_size_min': 10**400, 'group_size_max': -(10**400),
         'playoff_qualifier_count': 10**19},
        {'group_size_min', 'group_size_max', 'playoff_qualifier_count'},
    ),
}

_REFUSED = [
    pytest.param(b'not json', {''}, id='not_json'),
    pytest.param(b'\xff\xfe', {''}, id='not_utf8'),
    pytest.param(b' ' * (256 * 1024 + 1), {''}, id='too_large'),
    pytest.param(
        json.dumps({'format': 'lan_tournament.config', 'version': 2,
                    'tournament': {}}).encode(),
        {''},
        id='wrong_version',
    ),
    pytest.param(
        json.dumps({'format': 'lan_tournament.config', 'version': 1,
                    'tournament': {'name': 'Cup', 'category': 'MAIN',
                                   'image_url': 'https://example.test/x'}}
                   ).encode(),
        {''},
        id='unknown_key',
    ),
    pytest.param(_document(name='   '), {'name'}, id='blank_name'),
    pytest.param(
        _document(category='NOPE'), {'category'}, id='unknown_category'
    ),
    pytest.param(
        _document(game_format='NOPE', contestant_type='NOPE'),
        {'game_format', 'contestant_type'},
        id='unknown_enum_names',
    ),
    pytest.param(
        _document(min_players=20, max_players=10),
        {'max_players'},
        id='min_above_max',
    ),
    pytest.param(
        _document(max_players=2000), {'max_players'}, id='count_above_cap'
    ),
    *[
        pytest.param(
            _document(HIGHSCORE_FFA_PLAYOFFS_INPUT, **overrides),
            locations,
            id=f'hostile_{case_id}',
        )
        for case_id, (overrides, locations) in _HOSTILE_INTS.items()
    ],
]
# fmt: on


@pytest.mark.parametrize(('raw', 'locations'), _REFUSED)
def test_refused_import_writes_nothing(raw, locations):
    tournaments_before, log_entries_before = _counts()

    with patch.object(
        tournament_repository,
        'commit_session',
        wraps=tournament_repository.commit_session,
    ) as commit_session:
        result = tournament_config_service.check_import(raw)

    assert result.is_err()
    problems = result.unwrap_err()
    assert {p.location for p in problems} == locations
    commit_session.assert_not_called()
    assert _counts() == (tournaments_before, log_entries_before)


def test_import_duplicate_token_returns_duplicate_error(party_b, admin):
    check = tournament_config_service.check_import(_document()).unwrap()
    token = uuid4()
    first = tournament_config_service.import_tournament_config(
        party_b.id, check, admin.id, creation_token=token
    )
    assert first.is_ok()
    counts_after_first = _counts()

    second = tournament_config_service.import_tournament_config(
        party_b.id, check, admin.id, creation_token=token
    )

    assert second == Err(tournament_service.DUPLICATE_SUBMISSION_ERROR)
    assert _counts() == counts_after_first
    assert (
        _committed(
            'SELECT count(*) FROM lan_tournaments WHERE creation_token = :t',
            t=token,
        )[0][0]
        == 1
    )
