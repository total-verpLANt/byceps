"""
tests.integration.services.lan_tournament.test_seeding_board_seed_bands
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The playoff seeding board announces the open seed bands of the
qualification and reads the qualification no more often than before.
"""

from datetime import datetime, UTC
from itertools import count

import pytest

from byceps.database import db
from byceps.services.lan_tournament import (
    tournament_match_service,
    tournament_qualification_service,
    tournament_repository,
    tournament_seeding_service,
    tournament_service,
)
from byceps.services.lan_tournament.models import ContestantType
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.playoff import PlayoffReleaseMode
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipant,
    TournamentParticipantID,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.party.models import PartyID
from byceps.util.uuid import generate_uuid7


PARTY_ID = PartyID('lan-party-seeding-board-seed-bands')

_counter = count(1)


@pytest.fixture(scope='module')
def party(make_party, make_brand):
    brand = make_brand('seedingboardseedbandsbrand', 'Seeding Board Seed Bands')
    return make_party(brand, PARTY_ID, 'LAN Party Seeding Board Seed Bands')


@pytest.fixture(scope='module')
def users(make_user):
    return [make_user(f'SeedBoardBands{i}') for i in range(4)]


@pytest.fixture
def make_tournament(party, users):
    created = []

    def _make(*, started=True):
        tournament, _ = tournament_service.create_tournament(
            PARTY_ID,
            f'Seeding Board Seed Bands {next(_counter)}',
            contestant_type=ContestantType.SOLO,
            game_format=GameFormat.ONE_V_ONE,
            elimination_mode=EliminationMode.ROUND_ROBIN,
            tournament_status=TournamentStatus.REGISTRATION_CLOSED,
            playoff_game_format=GameFormat.ONE_V_ONE,
            playoff_elimination_mode=EliminationMode.SINGLE_ELIMINATION,
            playoff_group_count=2,
            playoff_qualifiers_per_group=2,
            playoff_release_mode=PlayoffReleaseMode.MANUAL,
        ).unwrap()
        created.append(tournament)
        for user in users:
            tournament_repository.create_participant(
                TournamentParticipant(
                    id=TournamentParticipantID(generate_uuid7()),
                    user_id=user.id,
                    tournament_id=tournament.id,
                    substitute_player=False,
                    team_id=None,
                    created_at=datetime.now(UTC),
                )
            )
        db.session.commit()
        if started:
            tournament_match_service.generate_round_robin_bracket(
                tournament.id
            ).unwrap()
            tournament_service.change_status(
                tournament.id, TournamentStatus.ONGOING, users[0].id
            ).unwrap()
        return tournament

    yield _make
    db.session.rollback()
    for tournament in created:
        if tournament_repository.find_tournament(tournament.id) is not None:
            tournament_service.delete_tournament(tournament.id)


@pytest.fixture
def open_seed_bands(make_tournament, users):
    """Return a tournament that qualified with two seed bands left open."""
    tournament = make_tournament()
    matches = tournament_repository.get_matches_for_tournament(tournament.id)
    contestants = tournament_repository.get_contestants_for_tournament(
        tournament.id
    )
    for match in matches:
        ids = [c.participant_id for c in contestants[match.id]]
        low, _ = sorted(ids, key=str)
        scores = (1, 0) if ids[0] == low else (0, 1)
        tournament_match_service.admin_set_and_confirm_match(
            match.id, users[0].id, dict(zip(ids, scores, strict=True))
        ).unwrap()
    state = _qualification(tournament)
    assert state.ready
    assert state.blockers == ()
    assert len(state.seeding_ties) == 2
    return tournament


def _qualification(tournament):
    return tournament_qualification_service.get_qualification(
        tournament.id
    ).unwrap()


class _Spy:
    def __init__(self, monkeypatch, owner, name):
        self.count = 0
        original = getattr(owner, name)

        def counted(*args, **kwargs):
            self.count += 1
            return original(*args, **kwargs)

        monkeypatch.setattr(owner, name, counted)


@pytest.fixture
def qualification_reads(monkeypatch):
    return _Spy(
        monkeypatch, tournament_qualification_service, 'get_qualification'
    )


def test_playoff_board_announces_the_open_seed_bands(open_seed_bands):
    tournament_seeding_service.ensure_playoff_draft(open_seed_bands.id).unwrap()

    board = tournament_seeding_service.get_board(
        open_seed_bands.id, 'playoff'
    ).unwrap()

    state = _qualification(open_seed_bands)
    assert len(board.seed_band_ties) == 2
    assert board.seed_band_ties == state.seeding_ties
    assert {tie.scope for tie in board.seed_band_ties} == {'crossover'}


def test_playoff_board_reads_the_qualification_once(
    open_seed_bands, qualification_reads
):
    tournament_seeding_service.ensure_playoff_draft(open_seed_bands.id).unwrap()
    qualification_reads.count = 0

    board = tournament_seeding_service.get_board(
        open_seed_bands.id, 'playoff'
    ).unwrap()

    assert board.seed_band_ties
    assert qualification_reads.count == 1


def test_playoff_board_takes_the_bands_of_a_passed_qualification(
    open_seed_bands, qualification_reads
):
    tournament_seeding_service.ensure_playoff_draft(open_seed_bands.id).unwrap()
    state = _qualification(open_seed_bands)
    qualification_reads.count = 0

    board = tournament_seeding_service.get_board(
        open_seed_bands.id, 'playoff', qualification=state
    ).unwrap()

    assert board.seed_band_ties == state.seeding_ties
    assert qualification_reads.count == 0


def test_playoff_board_has_no_bands_once_the_orga_decided_them(
    open_seed_bands, users
):
    tournament_seeding_service.ensure_playoff_draft(open_seed_bands.id).unwrap()
    for tie in _qualification(open_seed_bands).seeding_ties:
        tournament_qualification_service.save_decision(
            open_seed_bands.id,
            'crossover',
            list(reversed(tie.contestant_ids)),
            reason='seeded by the orga',
            initiator_id=users[0].id,
        ).unwrap()
    assert _qualification(open_seed_bands).seeding_ties == ()

    board = tournament_seeding_service.get_board(
        open_seed_bands.id, 'playoff'
    ).unwrap()

    assert board.seed_band_ties == ()


def test_layout_actions_stay_usable_while_a_seed_band_is_open(
    open_seed_bands, users
):
    drafted = tournament_seeding_service.ensure_playoff_draft(
        open_seed_bands.id
    ).unwrap()

    swapped = tournament_seeding_service.apply_action(
        open_seed_bands.id,
        'playoff',
        tournament_seeding_service.Swap(0, 1),
        expected_version=drafted.version,
        initiator_id=users[0].id,
    )

    assert swapped.is_ok(), swapped.unwrap_err()
    assert swapped.unwrap().version == drafted.version + 1
    assert len(swapped.unwrap().seed_band_ties) == 2


def test_initial_board_has_no_bands_and_reads_no_qualification(
    make_tournament, users, qualification_reads
):
    tournament = make_tournament(started=False)

    board = tournament_seeding_service.get_board(
        tournament.id, 'initial', initiator_id=users[0].id
    ).unwrap()

    assert board.seed_band_ties == ()
    assert qualification_reads.count == 0
