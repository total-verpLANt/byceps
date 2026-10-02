"""
tests.integration.services.lan_tournament.test_seeding_board_queries
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

A seeding call reads the plan, the roster, the lock and the phase-2
progress once, however many parts of the board need them.
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
from byceps.services.lan_tournament.models.bracket import Bracket
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


PARTY_ID = PartyID('lan-party-seeding-board-queries')
SE = EliminationMode.SINGLE_ELIMINATION
DE = EliminationMode.DOUBLE_ELIMINATION

_counter = count(1)

# mode, pool of the round-1 draft, its target
FFA_CASES = [
    pytest.param(SE, None, 'ffa:SE:1', id='se'),
    pytest.param(DE, Bracket.WINNERS, 'ffa:WB:1', id='de'),
]


@pytest.fixture(scope='module')
def party(make_party, make_brand):
    brand = make_brand('seedingboardqueriesbrand', 'Seeding Board Queries')
    return make_party(brand, PARTY_ID, 'LAN Party Seeding Board Queries')


@pytest.fixture(scope='module')
def users(make_user):
    return [make_user(f'SeedBoardQueries{i:02d}') for i in range(16)]


@pytest.fixture(scope='module')
def admin(make_user):
    return make_user('SeedBoardQueriesAdmin')


@pytest.fixture
def created(party):
    tournaments = []
    yield tournaments
    db.session.rollback()
    for tournament in tournaments:
        if tournament_repository.find_tournament(tournament.id) is not None:
            tournament_service.delete_tournament(tournament.id)


def _join(tournament, users):
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


def _members(match):
    return [
        str(c.participant_id)
        for c in tournament_match_service.get_contestants_for_match(match.id)
    ]


def _play(match, admin):
    order = _members(match)
    tournament_match_service.set_ffa_placements(
        match.id, {cid: i + 1 for i, cid in enumerate(order)}
    ).unwrap()
    tournament_match_service.confirm_ffa_match(match.id, admin.id).unwrap()


@pytest.fixture
def ffa_round_draft(created, users, admin):
    """Return a 16 player FFA draft for round 1; round 0 is confirmed."""

    def _make(mode, pool):
        tournament, _ = tournament_service.create_tournament(
            PARTY_ID,
            f'Seeding Board Queries {next(_counter)}',
            game_format=GameFormat.FREE_FOR_ALL,
            elimination_mode=mode,
            contestant_type=ContestantType.SOLO,
            tournament_status=TournamentStatus.REGISTRATION_CLOSED,
            max_players=16,
            group_size_min=4,
            group_size_max=4,
            advancement_count=2,
            point_table=[10, 6, 3, 1],
        ).unwrap()
        created.append(tournament)
        _join(tournament, users)
        tournament_match_service.generate_ffa_round(
            tournament.id, bracket=pool, initiator_id=admin.id
        ).unwrap()
        tournament_service.change_status(
            tournament.id, TournamentStatus.ONGOING, admin.id
        ).unwrap()
        for match in tournament_repository.get_matches_for_round(
            tournament.id, 0, bracket=pool
        ):
            _play(match, admin)
        target = tournament_seeding_service.prepare_ffa_round_draft(
            tournament.id, pool=pool, initiator_id=admin.id
        ).unwrap()
        return tournament, target

    return _make


class _Calls:
    def __init__(self, monkeypatch):
        self.monkeypatch = monkeypatch
        self.counts = {}

    def spy(self, owner, name):
        original = getattr(owner, name)
        self.counts[name] = 0

        def counted(*args, **kwargs):
            self.counts[name] += 1
            return original(*args, **kwargs)

        self.monkeypatch.setattr(owner, name, counted)

    def reset(self):
        self.counts = dict.fromkeys(self.counts, 0)


@pytest.fixture
def calls(monkeypatch):
    return _Calls(monkeypatch)


def _spy_ffa(calls):
    calls.spy(tournament_match_service, 'plan_ffa_advance')
    calls.spy(tournament_seeding_service, '_ffa_lock_reason')
    calls.spy(tournament_seeding_service, '_roster')


@pytest.mark.parametrize(('mode', 'pool', 'target'), FFA_CASES)
def test_ffa_round_board_plans_once(
    ffa_round_draft, calls, admin, mode, pool, target
):
    tournament, drafted = ffa_round_draft(mode, pool)
    assert drafted == target
    _spy_ffa(calls)

    board = tournament_seeding_service.get_board(
        tournament.id, target, initiator_id=admin.id
    )

    assert board.is_ok(), board.unwrap_err()
    assert calls.counts == {
        'plan_ffa_advance': 1,
        '_ffa_lock_reason': 1,
        '_roster': 1,
    }


@pytest.mark.parametrize(('mode', 'pool', 'target'), FFA_CASES)
def test_ffa_round_action_plans_once(
    ffa_round_draft, calls, admin, mode, pool, target
):
    tournament, _ = ffa_round_draft(mode, pool)
    board = tournament_seeding_service.get_board(
        tournament.id, target, initiator_id=admin.id
    ).unwrap()
    _spy_ffa(calls)

    result = tournament_seeding_service.apply_action(
        tournament.id,
        target,
        tournament_seeding_service.Swap(0, 3),
        expected_version=board.version,
        initiator_id=admin.id,
    )

    assert result.is_ok(), result.unwrap_err()
    assert calls.counts['plan_ffa_advance'] == 1
    assert calls.counts['_ffa_lock_reason'] == 1


@pytest.fixture
def released_playoffs(created, users, admin, party):
    """Return a round robin tournament whose single elimination playoffs ran."""
    tournament, _ = tournament_service.create_tournament(
        PARTY_ID,
        f'Seeding Board Queries RR {next(_counter)}',
        contestant_type=ContestantType.SOLO,
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.ROUND_ROBIN,
        tournament_status=TournamentStatus.REGISTRATION_CLOSED,
        playoff_game_format=GameFormat.ONE_V_ONE,
        playoff_elimination_mode=SE,
        playoff_group_count=2,
        playoff_qualifiers_per_group=2,
        playoff_release_mode=PlayoffReleaseMode.MANUAL,
    ).unwrap()
    created.append(tournament)
    _join(tournament, users[:8])
    tournament_match_service.generate_round_robin_bracket(
        tournament.id
    ).unwrap()
    tournament_service.change_status(
        tournament.id, TournamentStatus.ONGOING, admin.id
    ).unwrap()
    contestants = tournament_repository.get_contestants_for_tournament(
        tournament.id
    )
    for match in tournament_repository.get_matches_for_tournament(
        tournament.id
    ):
        if match.phase != 1:
            continue
        low, high = sorted(
            (c.participant_id for c in contestants[match.id]), key=str
        )
        tournament_match_service.admin_set_and_confirm_match(
            match.id, admin.id, {low: match.group_order + 1, high: 0}
        ).unwrap()
    board = tournament_seeding_service.ensure_playoff_draft(
        tournament.id
    ).unwrap()
    tournament_qualification_service.release_playoffs(
        tournament.id, expected_version=board.version, initiator_id=admin.id
    ).unwrap()
    return tournament


def test_playoff_board_reads_phase_two_once(released_playoffs, calls, admin):
    calls.spy(tournament_qualification_service, 'get_phase_two_progress')

    board = tournament_seeding_service.get_board(
        released_playoffs.id, 'playoff'
    )

    assert board.is_ok(), board.unwrap_err()
    assert calls.counts == {'get_phase_two_progress': 1}


def test_playoff_board_takes_a_precomputed_qualification(
    released_playoffs, calls
):
    state = tournament_qualification_service.get_qualification(
        released_playoffs.id
    ).unwrap()
    plain = tournament_seeding_service.get_board(
        released_playoffs.id, 'playoff'
    ).unwrap()
    calls.spy(tournament_qualification_service, 'get_qualification')

    shared = tournament_seeding_service.get_board(
        released_playoffs.id, 'playoff', qualification=state
    )

    assert shared.is_ok(), shared.unwrap_err()
    assert calls.counts == {'get_qualification': 0}
    assert shared.unwrap() == plain
