"""
tests.unit.services.lan_tournament.test_seeding_service
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from dataclasses import replace
from datetime import datetime, UTC
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest

from byceps.services.lan_tournament import (
    seed_code,
    tournament_seeding_domain_service as domain,
    tournament_seeding_service as svc,
)
from byceps.services.lan_tournament.models.contestant_type import (
    ContestantType,
)
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.seeding import SeedingFormat
from byceps.services.lan_tournament.dbmodels.seeding import (
    snapshot_from_json,
    snapshot_to_json,
)
from byceps.services.lan_tournament.models.tournament import (
    Tournament,
    TournamentID,
)
from byceps.services.lan_tournament.models.tournament_seeding import (
    TournamentSeeding,
    TournamentSeedingID,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.lan_tournament.tournament_domain_service import (
    FFA_NO_PROGRESS_MSGID,
    FFA_STALLS_MSGID,
    FfaDeadEnd,
)
from byceps.util.result import Err, Ok


TOURNAMENT_ID = TournamentID(UUID('00000000-0000-0000-0000-00000000f101'))
INITIATOR = UUID('00000000-0000-0000-0000-00000000a001')
NOW = datetime(2026, 1, 1, tzinfo=UTC)

CONFLICT = 'The seeding was changed by another orga. Reload the page.'

SE = GameFormat.ONE_V_ONE, EliminationMode.SINGLE_ELIMINATION
RR = GameFormat.ONE_V_ONE, EliminationMode.ROUND_ROBIN
FFA = GameFormat.FREE_FOR_ALL, EliminationMode.SINGLE_ELIMINATION


def _pid(i):
    return UUID(f'00000000-0000-0000-0000-{i:012d}')


def _tournament(mode=SE, status=TournamentStatus.REGISTRATION_CLOSED, **kw):
    fields = {
        'id': TOURNAMENT_ID,
        'party_id': 'party',
        'name': 'T',
        'game': None,
        'description': None,
        'image_url': None,
        'ruleset': None,
        'start_time': None,
        'created_at': NOW,
        'min_players': None,
        'max_players': None,
        'min_teams': None,
        'max_teams': None,
        'min_players_in_team': None,
        'max_players_in_team': None,
        'contestant_type': ContestantType.SOLO,
        'tournament_status': status,
        'game_format': mode[0],
        'elimination_mode': mode[1],
    }
    fields.update(kw)
    return Tournament(**fields)


class World:
    """In-memory stand-in for both repositories."""

    def __init__(self, tournament, active):
        self.tournament = tournament
        self.active = list(active)
        self.seedings = {}

    def participants(self, tournament_id):
        return [SimpleNamespace(id=i, user_id=i) for i in self.active]

    @staticmethod
    def users(user_ids):
        return {
            i: SimpleNamespace(screen_name=f'user-{i.int}') for i in user_ids
        }

    def find(self, tournament_id, target):
        return self.seedings.get((tournament_id, target))

    def create(self, seeding):
        self.seedings[(seeding.tournament_id, seeding.target)] = seeding

    def update(
        self,
        seeding_id,
        *,
        seed_code,
        expected_version,
        roster_snapshot,
        updated_by,
        now,
    ):
        for key, seeding in self.seedings.items():
            if seeding.id != seeding_id:
                continue
            if seeding.version != expected_version:
                return Err(CONFLICT)
            updated = replace(
                seeding,
                seed_code=seed_code,
                roster_snapshot=tuple(roster_snapshot),
                version=seeding.version + 1,
                updated_by=updated_by,
                updated_at=now,
            )
            self.seedings[key] = updated
            return Ok(updated)
        return Err(CONFLICT)


@pytest.fixture
def make_world():
    patches = []
    logs = MagicMock()
    repo = MagicMock()
    seeding_repo = MagicMock()
    users = MagicMock()

    def build(tournament=None, n=8):
        world = World(
            tournament or _tournament(),
            [_pid(i) for i in range(1, n + 1)],
        )
        repo.get_tournament.side_effect = lambda tid, *, fresh=False: world.tournament
        repo.get_participants_for_tournament.side_effect = world.participants
        users.get_users_indexed_by_id.side_effect = world.users
        seeding_repo.find_seeding.side_effect = world.find
        seeding_repo.find_seeding_for_update.side_effect = world.find
        seeding_repo.create_seeding.side_effect = world.create
        seeding_repo.update_seeding_code.side_effect = world.update
        return world

    for name, mock in (
        ('tournament_repository', repo),
        ('tournament_seeding_repository', seeding_repo),
        ('create_log_entry', logs),
        ('user_service', users),
    ):
        p = patch.object(svc, name, mock)
        p.start()
        patches.append(p)

    build.repo = repo
    build.logs = logs
    yield build
    for p in patches:
        p.stop()


def _event_types(logs):
    return [call.args[0] for call in logs.call_args_list]


def test_get_board_before_registration_closed_errs(make_world):
    world = make_world(_tournament(status=TournamentStatus.REGISTRATION_OPEN))

    result = svc.get_board(TOURNAMENT_ID)

    assert result == Err('The seeding opens once registration is closed.')
    assert world.seedings == {}
    make_world.logs.assert_not_called()


def test_apply_swap_changes_code_and_logs(make_world):
    world = make_world()
    board = svc.get_board(TOURNAMENT_ID).unwrap()
    make_world.logs.reset_mock()

    result = svc.apply_action(
        TOURNAMENT_ID,
        'initial',
        svc.Swap(0, 1),
        expected_version=board.version,
        initiator_id=INITIATOR,
    )

    new = result.unwrap()
    before = board.state.layout
    assert new.state.layout[0] == before[1]
    assert new.state.layout[1] == before[0]
    assert new.fix_count == 1
    assert new.version == board.version + 1
    stored = world.seedings[(TOURNAMENT_ID, 'initial')]
    assert new.code == seed_code.format_seed_code(stored.seed_code)
    assert _event_types(make_world.logs) == ['seeding-swapped']
    entry = make_world.logs.call_args
    assert entry.kwargs['commit'] is False
    assert entry.args[2] == INITIATOR
    make_world.repo.commit_session.assert_called()


def test_apply_action_version_conflict(make_world):
    world = make_world()
    board = svc.get_board(TOURNAMENT_ID).unwrap()
    code_before = world.seedings[(TOURNAMENT_ID, 'initial')].seed_code
    make_world.logs.reset_mock()
    make_world.repo.commit_session.reset_mock()

    result = svc.apply_action(
        TOURNAMENT_ID,
        'initial',
        svc.Swap(0, 1),
        expected_version=board.version + 1,
        initiator_id=INITIATOR,
    )

    assert result == Err(CONFLICT)
    assert world.seedings[(TOURNAMENT_ID, 'initial')].seed_code == code_before
    make_world.logs.assert_not_called()
    make_world.repo.commit_session.assert_not_called()
    make_world.repo.rollback_session.assert_called()


def test_replay_other_format_rejected(make_world):
    world = make_world()
    board = svc.get_board(TOURNAMENT_ID).unwrap()
    ids = [str(i) for i in world.active]
    rr_state = svc.domain.initial_state(
        svc.SeedingFormat.ROUND_ROBIN, 1, ids, tier_count=1, draw_seed=7
    )
    foreign = svc._encode(rr_state)

    result = svc.apply_action(
        TOURNAMENT_ID,
        'initial',
        svc.Replay(foreign),
        expected_version=board.version,
        initiator_id=INITIATOR,
    )

    assert result == Err('This code belongs to a different tournament mode.')


def test_replay_same_format_restores_layout(make_world):
    make_world()
    board = svc.get_board(TOURNAMENT_ID).unwrap()
    swapped = svc.apply_action(
        TOURNAMENT_ID, 'initial', svc.Swap(0, 1),
        expected_version=board.version, initiator_id=INITIATOR,
    ).unwrap()  # fmt: skip

    result = svc.apply_action(
        TOURNAMENT_ID,
        'initial',
        svc.Replay(board.code),
        expected_version=swapped.version,
        initiator_id=INITIATOR,
    ).unwrap()

    assert result.state.layout == board.state.layout
    assert result.fix_count == 0


def test_stale_after_withdrawal(make_world):
    world = make_world(n=8)
    board = svc.get_board(TOURNAMENT_ID).unwrap()
    assert not board.stale
    leaver = world.active.pop()

    stale = svc.get_board(TOURNAMENT_ID).unwrap()

    assert stale.stale
    assert stale.stale_leavers == (f'user-{leaver.int}',)
    assert stale.stale_leaver_ids == (str(leaver),)
    assert stale.labels[str(leaver)] == f'user-{leaver.int}'
    assert stale.stale_joiners == ()
    assert stale.state.roster == board.state.roster

    blocked = svc.apply_action(
        TOURNAMENT_ID, 'initial', svc.Swap(0, 1),
        expected_version=stale.version, initiator_id=INITIATOR,
    )  # fmt: skip
    assert blocked == Err(svc.ERR_STALE)

    reseeded = svc.apply_action(
        TOURNAMENT_ID, 'initial', svc.ReseedKeepTiers(),
        expected_version=stale.version, initiator_id=INITIATOR,
    ).unwrap()  # fmt: skip
    assert not reseeded.stale
    assert set(reseeded.state.roster) == {str(i) for i in world.active}
    assert 'seeding-roster-reseeded' in _event_types(make_world.logs)


def test_stale_after_new_entrant_names_joiner(make_world):
    world = make_world(n=8)
    svc.get_board(TOURNAMENT_ID)
    world.active.append(_pid(99))

    stale = svc.get_board(TOURNAMENT_ID).unwrap()

    assert stale.stale
    assert stale.stale_joiners == (f'user-{_pid(99).int}',)
    assert stale.stale_joiner_ids == (str(_pid(99)),)
    assert stale.stale_leavers == ()


def test_reseed_marks_joiners_new(make_world):
    world = make_world(n=8)
    board = svc.get_board(TOURNAMENT_ID).unwrap()
    assert board.new_entrant_ids == ()
    world.active.append(_pid(99))
    stale = svc.get_board(TOURNAMENT_ID).unwrap()
    assert stale.new_entrant_ids == ()

    reseeded = svc.apply_action(
        TOURNAMENT_ID, 'initial', svc.ReseedKeepTiers(),
        expected_version=stale.version, initiator_id=INITIATOR,
    ).unwrap()  # fmt: skip

    assert not reseeded.stale
    assert reseeded.new_entrant_ids == (str(_pid(99)),)
    stored = world.seedings[(TOURNAMENT_ID, 'initial')].roster_snapshot
    assert {e.id for e in stored if e.joined_late} == {str(_pid(99))}

    swapped = svc.apply_action(
        TOURNAMENT_ID, 'initial', svc.Swap(0, 1),
        expected_version=reseeded.version, initiator_id=INITIATOR,
    ).unwrap()  # fmt: skip
    assert swapped.new_entrant_ids == (str(_pid(99)),)


def test_new_entrant_marker_follows_a_leaving_joiner(make_world):
    world = make_world(n=8)
    svc.get_board(TOURNAMENT_ID)
    world.active.append(_pid(99))
    stale = svc.get_board(TOURNAMENT_ID).unwrap()
    svc.apply_action(
        TOURNAMENT_ID, 'initial', svc.ReseedKeepTiers(),
        expected_version=stale.version, initiator_id=INITIATOR,
    ).unwrap()  # fmt: skip
    world.active.remove(_pid(99))
    stale = svc.get_board(TOURNAMENT_ID).unwrap()

    reseeded = svc.apply_action(
        TOURNAMENT_ID, 'initial', svc.ReseedKeepTiers(),
        expected_version=stale.version, initiator_id=INITIATOR,
    ).unwrap()  # fmt: skip

    assert reseeded.new_entrant_ids == ()


def test_snapshot_without_previous_clears_the_markers(make_world):
    make_world(n=4)
    state = svc.get_board(TOURNAMENT_ID).unwrap().state
    labels = {cid: cid for cid in state.roster}
    marked = svc._snapshot(state, labels, joiner_ids=[state.roster[0]])
    assert [e.joined_late for e in marked] == [True, False, False, False]

    kept = svc._snapshot(state, labels, previous=marked)
    assert kept == marked
    assert not any(e.joined_late for e in svc._snapshot(state, labels))


def test_legacy_snapshot_entries_are_not_new():
    assert svc.RosterEntry('a', 'A').joined_late is False


def test_stale_without_snapshot_falls_back_to_fresh_draw(make_world):
    world = make_world(n=8)
    svc.get_board(TOURNAMENT_ID)
    key = (TOURNAMENT_ID, 'initial')
    world.seedings[key] = replace(world.seedings[key], roster_snapshot=())
    world.active.pop()

    stale = svc.get_board(TOURNAMENT_ID).unwrap()

    assert stale.stale
    assert stale.stale_leavers == ()
    assert set(stale.state.roster) == {str(i) for i in world.active}


def test_every_write_stores_the_current_roster_snapshot(make_world):
    world = make_world(n=4)
    board = svc.get_board(TOURNAMENT_ID).unwrap()
    key = (TOURNAMENT_ID, 'initial')
    expected = tuple(
        svc.RosterEntry(str(i), f'user-{i.int}')
        for i in sorted(world.active, key=str)
    )
    assert world.seedings[key].roster_snapshot == expected

    svc.apply_action(
        TOURNAMENT_ID, 'initial', svc.Swap(0, 1),
        expected_version=board.version, initiator_id=INITIATOR,
    ).unwrap()  # fmt: skip

    assert world.seedings[key].roster_snapshot == expected


# fmt: off
@pytest.mark.parametrize(('n', 'param', 'tiers'), [
    (4,  8, 2),
    (8,  4, 2),
    (12, 4, 3),
    (16, 4, 4),
    (40, 4, 4),
])
# fmt: on
def test_initial_ffa_draft_splits_into_tiers(make_world, n, param, tiers):
    make_world(_tournament(FFA, group_size_max=param), n=n)

    board = svc.get_board(TOURNAMENT_ID).unwrap()

    assert board.state.tier_count == tiers
    assert set(board.state.tiers) == set(range(tiers))


def test_initial_tier_count_is_one_outside_ffa():
    fmt = SeedingFormat.SINGLE_ELIMINATION
    assert svc._initial_tier_count(fmt, 16, 4) == 1


def test_reseed_after_hard_removal_keeps_tiers(make_world):
    world = make_world(_tournament(FFA, group_size_max=4), n=8)
    board = svc.get_board(TOURNAMENT_ID).unwrap()
    tiered = svc.apply_action(
        TOURNAMENT_ID, 'initial', svc.SetTierCount(3),
        expected_version=board.version, initiator_id=INITIATOR,
    ).unwrap()  # fmt: skip
    tier_before = dict(
        zip(tiered.state.roster, tiered.state.tiers, strict=True)
    )
    leaver = world.active.pop(2)

    stale = svc.get_board(TOURNAMENT_ID).unwrap()
    assert stale.stale_leaver_ids == (str(leaver),)

    fresh = svc.apply_action(
        TOURNAMENT_ID, 'initial', svc.ReseedKeepTiers(),
        expected_version=stale.version, initiator_id=INITIATOR,
    ).unwrap()  # fmt: skip

    tier_after = dict(zip(fresh.state.roster, fresh.state.tiers, strict=True))
    assert str(leaver) not in tier_after
    assert tier_after == {
        cid: t for cid, t in tier_before.items() if cid != str(leaver)
    }


@pytest.mark.parametrize(
    'status',
    [TournamentStatus.ONGOING, TournamentStatus.COMPLETED],
)
def test_apply_action_locked_after_start(make_world, status):
    world = make_world()
    board = svc.get_board(TOURNAMENT_ID).unwrap()
    world.tournament = replace(world.tournament, tournament_status=status)

    result = svc.apply_action(
        TOURNAMENT_ID, 'initial', svc.Swap(0, 1),
        expected_version=board.version, initiator_id=INITIATOR,
    )  # fmt: skip

    assert result == Err(svc.ERR_LOCKED)
    readonly = svc.get_board(TOURNAMENT_ID).unwrap()
    assert readonly.locked_reason == svc.ERR_LOCKED
    assert readonly.generation is svc.GenerationStatus.LOCKED


def test_board_after_start_without_draft_errs(make_world):
    make_world(_tournament(status=TournamentStatus.ONGOING))

    assert svc.get_board(TOURNAMENT_ID) == Err(svc.ERR_NO_SEEDING)


def test_tier_actions_rejected_outside_ffa(make_world):
    make_world()
    board = svc.get_board(TOURNAMENT_ID).unwrap()

    result = svc.apply_action(
        TOURNAMENT_ID, 'initial', svc.SetTierCount(3),
        expected_version=board.version, initiator_id=INITIATOR,
    )  # fmt: skip

    assert result == Err(svc.ERR_NO_TIERS)


def test_ffa_tier_count_and_move_tier(make_world):
    make_world(_tournament(FFA, group_size_max=4), n=8)
    board = svc.get_board(TOURNAMENT_ID).unwrap()
    assert board.state.format is svc.SeedingFormat.FREE_FOR_ALL
    assert board.state.param == 4

    tiered = svc.apply_action(
        TOURNAMENT_ID, 'initial', svc.SetTierCount(3),
        expected_version=board.version, initiator_id=INITIATOR,
    ).unwrap()  # fmt: skip
    assert tiered.state.tier_count == 3
    assert tiered.balance is not None

    cid = tiered.state.seed_list[0]
    moved = svc.apply_action(
        TOURNAMENT_ID, 'initial', svc.MoveTier(cid, 1),
        expected_version=tiered.version, initiator_id=INITIATOR,
    ).unwrap()  # fmt: skip
    tier_of = dict(zip(moved.state.roster, moved.state.tiers, strict=True))
    assert tier_of[cid] == 1
    assert _event_types(make_world.logs)[-2:] == [
        'seeding-tiers-resized',
        'seeding-tier-changed',
    ]


def test_invalid_swap_index_errs_without_commit(make_world):
    make_world()
    board = svc.get_board(TOURNAMENT_ID).unwrap()
    make_world.repo.commit_session.reset_mock()

    result = svc.apply_action(
        TOURNAMENT_ID, 'initial', svc.Swap(0, 99),
        expected_version=board.version, initiator_id=INITIATOR,
    )  # fmt: skip

    assert result == Err(svc.ERR_INVALID_CHANGE)
    make_world.repo.commit_session.assert_not_called()


def test_plain_round_robin_single_group_is_not_a_problem(make_world):
    make_world(_tournament(RR), n=6)

    board = svc.get_board(TOURNAMENT_ID).unwrap()

    assert board.problems == ()


def test_two_byes_problem_carries_match_number(make_world):
    make_world(_tournament(SE), n=3)
    board = svc.get_board(TOURNAMENT_ID).unwrap()
    assert board.problems == ()

    broken_layout = (None, None, *board.state.layout[:2])
    broken = replace(board.state, layout=broken_layout)
    roster = svc._Roster(board.state.roster, (), ())

    msgids, params = svc._problems(broken, roster)

    assert msgids == [svc.domain.PROBLEM_TWO_BYES]
    assert params == [{'n': 1}]


@pytest.mark.parametrize(
    'target',
    ['ffa:XX:1', 'ffa:SE:x', 'ffa:SE:12345', 'ffa:SE:1:0', 'other'],
)
def test_unknown_target_errs(make_world, target):
    make_world()

    assert svc.get_board(TOURNAMENT_ID, target) == Err(svc.ERR_UNKNOWN_TARGET)
    assert svc.apply_action(
        TOURNAMENT_ID, target, svc.Redraw(),
        expected_version=1, initiator_id=INITIATOR,
    ) == Err(svc.ERR_UNKNOWN_TARGET)  # fmt: skip


@pytest.mark.parametrize(
    'target', ['playoff', 'ffa:SE:1', 'ffa:WB:2', 'ffa:LB:10']
)
def test_draft_target_is_known_but_has_no_draft_yet(make_world, target):
    make_world()

    assert svc.get_board(TOURNAMENT_ID, target) == Err(svc.ERR_NO_SEEDING)


def test_highscore_has_no_seeding(make_world):
    make_world(_tournament((GameFormat.HIGHSCORE, EliminationMode.NONE)), n=4)

    assert svc.get_board(TOURNAMENT_ID) == Err(svc.ERR_NO_FORMAT)


def test_single_contestant_has_no_seeding(make_world):
    make_world(n=1)

    assert svc.get_board(TOURNAMENT_ID) == Err(svc.ERR_TOO_FEW)


def test_generation_status_follows_generated_code(make_world):
    world = make_world()
    board = svc.get_board(TOURNAMENT_ID).unwrap()
    assert board.generation is svc.GenerationStatus.NOT_GENERATED

    key = (TOURNAMENT_ID, 'initial')
    stored = world.seedings[key]
    world.seedings[key] = replace(stored, generated_seed_code=stored.seed_code)
    assert (
        svc.get_board(TOURNAMENT_ID).unwrap().generation
        is svc.GenerationStatus.MATCHES
    )

    world.seedings[key] = replace(stored, generated_seed_code='Sother')
    assert (
        svc.get_board(TOURNAMENT_ID).unwrap().generation
        is svc.GenerationStatus.DIFFERS
    )


@pytest.mark.parametrize(
    'n',
    # fmt: off
    [0, 1, 5, 8, -1],
    # fmt: on
)
def test_tier_count_outside_2_to_4_refused(make_world, n):
    make_world(_tournament(FFA, group_size_max=4), n=8)
    board = svc.get_board(TOURNAMENT_ID).unwrap()

    result = svc.apply_action(
        TOURNAMENT_ID, 'initial', svc.SetTierCount(n),
        expected_version=board.version, initiator_id=INITIATOR,
    )  # fmt: skip

    assert result == Err(svc.ERR_TIER_COUNT)


@pytest.mark.parametrize('n', [2, 3, 4])
def test_tier_count_2_to_4_accepted(make_world, n):
    make_world(_tournament(FFA, group_size_max=4), n=8)
    board = svc.get_board(TOURNAMENT_ID).unwrap()

    result = svc.apply_action(
        TOURNAMENT_ID, 'initial', svc.SetTierCount(n),
        expected_version=board.version, initiator_id=INITIATOR,
    )  # fmt: skip

    assert result.unwrap().state.tier_count == n


@pytest.mark.parametrize(
    ('playoff', 'expected'),
    [(False, 'initial'), (True, 'playoff')],
)
def test_generator_is_told_the_seeding_target(make_world, playoff, expected):
    make_world()
    state = svc.get_board(TOURNAMENT_ID).unwrap().state

    with patch.object(
        svc.tournament_match_service, '_generate_single_elimination_impl'
    ) as generate:
        svc._run_generator(
            TOURNAMENT_ID, state, False, INITIATOR, playoff=playoff
        )

    assert generate.call_args.kwargs['seeding_target'] == expected


def test_format_for_accepts_1024_and_refuses_1025():
    tournament = _tournament(SE)
    ids = tuple(f'c{i:04d}' for i in range(1024))

    assert svc._format_for(
        tournament, svc._Roster(ids, {i: i for i in ids}, ())
    ).is_ok()
    over = (*ids, 'c1024')
    assert svc._format_for(
        tournament, svc._Roster(over, {i: i for i in over}, ())
    ) == Err(svc.ERR_TOO_MANY)

    state = domain.initial_state(
        SeedingFormat.SINGLE_ELIMINATION,
        0,
        ids,
        tier_count=1,
        draw_seed=7,
    )
    assert svc._decode_state(svc._encode(state), ids) == Ok(state)


def test_format_for_clamps_the_group_count_to_the_code_limit():
    tournament = _tournament(
        RR,
        playoff_game_format=GameFormat.ONE_V_ONE,
        playoff_group_count=300,
    )
    ids = tuple(f'c{i:04d}' for i in range(600))

    result = svc._format_for(
        tournament, svc._Roster(ids, {i: i for i in ids}, ())
    )

    assert result == Ok((SeedingFormat.ROUND_ROBIN, 255))
    state = domain.initial_state(
        SeedingFormat.ROUND_ROBIN, 255, ids, tier_count=1, draw_seed=7
    )
    assert svc._decode_state(svc._encode(state), ids) == Ok(state)


@pytest.mark.parametrize(
    ('size', 'expected'),
    # fmt: off
    [
        (2, SeedingFormat.SINGLE_ELIMINATION),
        (3, SeedingFormat.SINGLE_ELIMINATION),
        (4, SeedingFormat.DOUBLE_ELIMINATION),
    ],
    # fmt: on
)
def test_playoff_de_below_four_drafts_single_elimination(size, expected):
    tournament = _tournament(
        RR,
        playoff_game_format=GameFormat.ONE_V_ONE,
        playoff_elimination_mode=EliminationMode.DOUBLE_ELIMINATION,
        playoff_group_count=4,
        playoff_qualifiers_per_group=1,
    )
    ids = tuple(f'c{i}' for i in range(size))

    result = svc._format_for(
        tournament,
        svc._Roster(ids, {i: i for i in ids}, ()),
        svc.PLAYOFF_TARGET,
    )

    assert result == Ok((expected, 0))


def _playoff_state(swaps=()):
    ids = ('a', 'b', 'c', 'd')
    state = domain.initial_state(
        SeedingFormat.SINGLE_ELIMINATION,
        0,
        ids,
        tier_count=1,
        draw_seed=1,
        seed_list=ids,
    )
    for p, q in swaps:
        state = domain.swap_slots(state, p, q)
    origins = {
        'a': ('group:0', 1),
        'b': ('group:1', 1),
        'c': ('group:0', 2),
        'd': ('group:1', 2),
    }
    return state, svc._Roster(ids, {i: i for i in ids}, (), origins)


def test_separate_swaps_a_same_group_pairing_apart():
    state, roster = _playoff_state(swaps=[(1, 3)])
    assert state.layout[:2] == ('a', 'c')

    result = svc._separate(state, roster)

    assert result.is_ok()
    layout = result.unwrap().layout
    groups = {cid: roster.origins[cid][0] for cid in layout}
    assert [groups[layout[i]] != groups[layout[i + 1]] for i in (0, 2)] == [
        True,
        True,
    ]
    assert sorted(layout) == ['a', 'b', 'c', 'd']


def test_separate_refuses_when_nothing_pairs_one_group():
    state, roster = _playoff_state()

    result = svc._separate(state, roster)

    assert result == Err(svc.ERR_NOTHING_TO_SEPARATE)


def test_separate_refuses_when_no_swap_separates_all_pairs():
    state, roster = _playoff_state(swaps=[(1, 3)])
    one_group = svc._Roster(
        roster.ids,
        roster.labels,
        (),
        {cid: ('group:0', rank) for cid, (_, rank) in roster.origins.items()},
    )

    result = svc._separate(state, one_group)

    assert result == Err(svc.ERR_SEPARATE_STUCK)


def test_separate_refuses_without_group_origins():
    state, roster = _playoff_state(swaps=[(1, 3)])

    result = svc._separate(
        state, svc._Roster(roster.ids, roster.labels, (), {})
    )

    assert result == Err(svc.ERR_INVALID_CHANGE)


def test_separate_is_logged_as_its_own_event():
    state, _ = _playoff_state()

    assert svc._event_type(svc.Separate(), state) == 'seeding-separated'


def test_origin_labels_name_the_group_letter_and_rank():
    state, roster = _playoff_state()

    assert svc._origin_labels(roster, state) == {
        'a': 'A1',
        'b': 'B1',
        'c': 'A2',
        'd': 'B2',
    }


def test_origin_labels_skip_entrants_without_a_group():
    state, roster = _playoff_state()
    lobby = svc._Roster(
        roster.ids, roster.labels, (), {'a': ('leaderboard', 1)}
    )

    assert svc._origin_labels(lobby, state) == {}
    assert svc._same_group_matches(lobby, state) == ()


# -------------------------------------------------------------------- #
# the draft follows the tournament structure

DE = GameFormat.ONE_V_ONE, EliminationMode.DOUBLE_ELIMINATION


def _restructure(world, **changes):
    world.tournament = replace(world.tournament, **changes)


def _rr_playoff(groups):
    return _tournament(
        RR,
        playoff_game_format=GameFormat.ONE_V_ONE,
        playoff_elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        playoff_group_count=groups,
        playoff_qualifiers_per_group=2,
    )


def _reseed(board):
    return svc.apply_action(
        TOURNAMENT_ID, 'initial', svc.ReseedKeepTiers(),
        expected_version=board.version, initiator_id=INITIATOR,
    )  # fmt: skip


def _stored_decoded(world):
    stored = world.seedings[(TOURNAMENT_ID, 'initial')]
    return seed_code.decode_seed_code(stored.seed_code).unwrap()


def _structure_cases():
    return [
        pytest.param(
            _tournament(SE),
            {'elimination_mode': EliminationMode.DOUBLE_ELIMINATION},
            (SeedingFormat.DOUBLE_ELIMINATION, 0),
            id='se-to-de',
        ),
        pytest.param(
            _rr_playoff(2),
            {'playoff_group_count': 4},
            (SeedingFormat.ROUND_ROBIN, 4),
            id='rr-groups-2-to-4',
        ),
        pytest.param(
            _tournament(FFA, group_size_max=4),
            {'group_size_max': 2},
            (SeedingFormat.FREE_FOR_ALL, 2),
            id='ffa-lobby-size',
        ),
    ]


@pytest.mark.parametrize(
    ('tournament', 'change', 'expected'), _structure_cases()
)
def test_structure_change_makes_the_board_stale_with_a_fresh_draw(
    make_world, tournament, change, expected
):
    world = make_world(tournament)
    board = svc.get_board(TOURNAMENT_ID).unwrap()
    assert not board.stale
    _restructure(world, **change)

    stale = svc.get_board(TOURNAMENT_ID).unwrap()

    assert stale.stale
    assert stale.stale_structure
    assert (stale.state.format, stale.state.param) == expected
    assert set(stale.state.roster) == {str(i) for i in world.active}
    assert stale.stale_leavers == ()
    assert stale.stale_joiners == ()
    assert stale.problems == ()


@pytest.mark.parametrize(
    ('tournament', 'change', 'expected'), _structure_cases()
)
def test_structure_change_blocks_edits_until_reseeded(
    make_world, tournament, change, expected
):
    world = make_world(tournament)
    board = svc.get_board(TOURNAMENT_ID).unwrap()
    _restructure(world, **change)

    blocked = svc.apply_action(
        TOURNAMENT_ID, 'initial', svc.Swap(0, 1),
        expected_version=board.version, initiator_id=INITIATOR,
    )  # fmt: skip
    assert blocked == Err(svc.ERR_STRUCTURE_CHANGED)

    reseeded = _reseed(board).unwrap()

    assert not reseeded.stale
    assert not reseeded.stale_structure
    assert (reseeded.state.format, reseeded.state.param) == expected
    decoded = _stored_decoded(world)
    assert (decoded.format, decoded.param) == expected
    assert not svc.get_board(TOURNAMENT_ID).unwrap().stale


def test_structure_change_keeps_ffa_tiers(make_world):
    world = make_world(_tournament(FFA, group_size_max=4), n=8)
    board = svc.get_board(TOURNAMENT_ID).unwrap()
    tiered = svc.apply_action(
        TOURNAMENT_ID, 'initial', svc.SetTierCount(3),
        expected_version=board.version, initiator_id=INITIATOR,
    ).unwrap()  # fmt: skip
    _restructure(world, group_size_max=2)

    stale = svc.get_board(TOURNAMENT_ID).unwrap()

    assert stale.stale_structure
    assert stale.state.tier_count == 3
    assert stale.state.tiers == tiered.state.tiers
    assert stale.state.seed_list == tiered.state.seed_list


def test_structure_change_drops_tiers_a_format_cannot_hold(make_world):
    world = make_world(_tournament(FFA, group_size_max=4), n=8)
    board = svc.get_board(TOURNAMENT_ID).unwrap()
    svc.apply_action(
        TOURNAMENT_ID, 'initial', svc.SetTierCount(3),
        expected_version=board.version, initiator_id=INITIATOR,
    ).unwrap()  # fmt: skip
    _restructure(
        world,
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
    )

    stale = svc.get_board(TOURNAMENT_ID).unwrap()

    assert stale.stale_structure
    assert stale.state.format is SeedingFormat.SINGLE_ELIMINATION
    assert stale.state.tier_count == 1


def test_structure_change_with_a_roster_change_names_both(make_world):
    world = make_world(_tournament(FFA, group_size_max=4), n=8)
    board = svc.get_board(TOURNAMENT_ID).unwrap()
    svc.apply_action(
        TOURNAMENT_ID, 'initial', svc.SetTierCount(3),
        expected_version=board.version, initiator_id=INITIATOR,
    ).unwrap()  # fmt: skip
    leaver = world.active.pop()
    _restructure(world, group_size_max=2)

    stale = svc.get_board(TOURNAMENT_ID).unwrap()

    assert stale.stale_structure
    assert stale.stale_leaver_ids == (str(leaver),)
    assert stale.state.param == 2
    assert stale.state.tier_count == 3
    assert set(stale.state.roster) == {str(i) for i in world.active}


def test_roster_change_alone_is_not_a_structure_change(make_world):
    world = make_world(n=8)
    svc.get_board(TOURNAMENT_ID)
    world.active.pop()

    stale = svc.get_board(TOURNAMENT_ID).unwrap()

    assert stale.stale
    assert not stale.stale_structure


def test_generate_refuses_a_draft_of_another_structure(make_world):
    world = make_world(_tournament(SE))
    board = svc.get_board(TOURNAMENT_ID).unwrap()
    _restructure(world, elimination_mode=EliminationMode.DOUBLE_ELIMINATION)
    make_world.repo.commit_session.reset_mock()
    make_world.logs.reset_mock()

    with patch.object(svc, '_run_generator') as run:
        result = svc.generate_from_seeding(
            TOURNAMENT_ID,
            expected_version=board.version,
            initiator_id=INITIATOR,
        )

    assert result == Err(svc.ERR_STRUCTURE_CHANGED)
    run.assert_not_called()
    make_world.logs.assert_not_called()
    make_world.repo.commit_session.assert_not_called()
    make_world.repo.rollback_session.assert_called()
    make_world.repo.get_tournament.assert_called_with(TOURNAMENT_ID, fresh=True)


def test_the_structure_change_does_not_stale_a_locked_board(make_world):
    world = make_world(_tournament(SE))
    svc.get_board(TOURNAMENT_ID)
    _restructure(
        world,
        elimination_mode=EliminationMode.DOUBLE_ELIMINATION,
        tournament_status=TournamentStatus.ONGOING,
    )

    board = svc.get_board(TOURNAMENT_ID).unwrap()

    assert not board.stale
    assert board.state.format is SeedingFormat.SINGLE_ELIMINATION


# -------------------------------------------------------------------- #
# start check


def _mark_generated(world):
    key = (TOURNAMENT_ID, 'initial')
    stored = world.seedings[key]
    world.seedings[key] = replace(stored, generated_seed_code=stored.seed_code)


def test_start_check_passes_when_the_structure_is_unchanged(make_world):
    world = make_world(_tournament(SE))
    svc.get_board(TOURNAMENT_ID)
    _mark_generated(world)

    assert svc.start_violations(TOURNAMENT_ID) == []


@pytest.mark.parametrize(
    ('tournament', 'change', 'expected'), _structure_cases()
)
def test_start_check_refuses_a_generation_of_another_structure(
    make_world, tournament, change, expected
):
    world = make_world(tournament)
    svc.get_board(TOURNAMENT_ID)
    _mark_generated(world)
    _restructure(world, **change)

    assert svc.start_violations(TOURNAMENT_ID) == [
        svc.ERR_STRUCTURE_CHANGED_AFTER_GENERATION
    ]


def test_start_check_passes_after_regenerating_in_the_new_structure(
    make_world,
):
    world = make_world(_tournament(SE))
    board = svc.get_board(TOURNAMENT_ID).unwrap()
    _mark_generated(world)
    _restructure(world, elimination_mode=EliminationMode.DOUBLE_ELIMINATION)
    assert svc.start_violations(TOURNAMENT_ID)
    _reseed(board).unwrap()

    _mark_generated(world)

    assert svc.start_violations(TOURNAMENT_ID) == []


# -------------------------------------------------------------------- #
# replay decodes before the tournament lock


def _replay_spy(make_world, monkeypatch):
    calls = []
    real_decode = seed_code.decode_seed_code
    monkeypatch.setattr(
        svc.seed_code,
        'decode_seed_code',
        lambda raw: (calls.append('decode'), real_decode(raw))[1],
    )
    make_world.repo.lock_tournament_for_update.side_effect = lambda tid: (
        calls.append('lock')
    )
    return calls


def test_replay_decodes_before_the_lock(make_world, monkeypatch):
    make_world()
    board = svc.get_board(TOURNAMENT_ID).unwrap()
    calls = _replay_spy(make_world, monkeypatch)

    result = svc.apply_action(
        TOURNAMENT_ID,
        'initial',
        svc.Replay(board.code),
        expected_version=board.version,
        initiator_id=INITIATOR,
    )

    assert result.is_ok()
    assert calls[0] == 'decode'
    assert calls.index('decode') < calls.index('lock')


def test_replay_of_an_over_long_code_never_takes_the_lock(
    make_world, monkeypatch
):
    make_world()
    board = svc.get_board(TOURNAMENT_ID).unwrap()
    calls = _replay_spy(make_world, monkeypatch)
    make_world.repo.commit_session.reset_mock()
    monkeypatch.setattr(
        seed_code,
        '_fold_digits',
        lambda digits: pytest.fail('fold reached'),
    )

    result = svc.apply_action(
        TOURNAMENT_ID,
        'initial',
        svc.Replay('S' + '2' * seed_code.MAX_CODE_LENGTH),
        expected_version=board.version,
        initiator_id=INITIATOR,
    )

    assert result == Err('The seed code is too long.')
    assert calls == ['decode']
    make_world.repo.commit_session.assert_not_called()


def test_reseed_with_unchanged_roster_keeps_the_layout():
    ids = tuple(f'c{i:02d}' for i in range(8))
    roster = svc._Roster(ids, {i: i for i in ids}, ())
    state = domain.swap_slots(
        domain.initial_state(
            SeedingFormat.SINGLE_ELIMINATION,
            0,
            ids,
            tier_count=1,
            draw_seed=7,
        ),
        0,
        1,
    )
    fmt = SeedingFormat.SINGLE_ELIMINATION

    kept = svc._apply(state, svc.ReseedKeepTiers(), roster, fmt, 0)
    assert kept == Ok(state)

    smaller = svc._Roster(ids[:-1], {i: i for i in ids[:-1]}, ())
    shrunk = svc._apply(state, svc.ReseedKeepTiers(), smaller, fmt, 0)
    assert shrunk.unwrap().roster == smaller.ids
    assert shrunk.unwrap() != state


def test_split_groups_orders_each_group_by_contestant_id():
    state = SimpleNamespace(
        format=SeedingFormat.FREE_FOR_ALL,
        param=2,
        roster=('a', 'b', 'c', 'd'),
        layout=('d', 'a', 'c', 'b'),
    )

    assert svc._split_groups(state) == [['a', 'd'], ['b', 'c']]


_PREFILL_IDS = ('c0', 'c1', 'c2', 'c3', 'c4', 'c5')
_PREFILL_ORIGIN = {
    'c0': 'group:0',
    'c1': 'group:1',
    'c2': 'group:0',
    'c3': 'group:1',
    'c4': 'group:0',
    'c5': 'group:1',
}


def _prefill_roster():
    origins = {
        cid: (_PREFILL_ORIGIN[cid], i // 2 + 1)
        for i, cid in enumerate(_PREFILL_IDS)
    }
    return svc._Roster(
        _PREFILL_IDS,
        {i: i.upper() for i in _PREFILL_IDS},
        (),
        origins,
        prefill_order=_PREFILL_IDS,
    )


def _prefill_state(order=_PREFILL_IDS, draw_seed=7):
    return svc._prefill_playoff_state(
        SeedingFormat.SINGLE_ELIMINATION,
        0,
        order,
        _PREFILL_ORIGIN,
        draw_seed=draw_seed,
    )


def _drafted(state, snapshot):
    return TournamentSeeding(
        id=TournamentSeedingID(UUID(int=1)),
        tournament_id=TOURNAMENT_ID,
        target='playoff',
        seed_code=svc._encode(state),
        version=1,
        generated_seed_code=None,
        generated_at=None,
        updated_by=None,
        created_at=NOW,
        updated_at=NOW,
        roster_snapshot=tuple(snapshot),
    )


def test_snapshot_carries_the_prefill_baseline():
    roster = _prefill_roster()
    state = _prefill_state()

    written = svc._snapshot(state, roster.labels, prefill=roster)

    assert {e.id: (e.prefill_index, e.origin) for e in written} == {
        cid: (i, _PREFILL_ORIGIN[cid]) for i, cid in enumerate(_PREFILL_IDS)
    }
    assert svc._prefill_baseline(_drafted(state, written)) == _PREFILL_IDS
    # Without a prefill the baseline is carried by ID, and survives JSON.
    carried = svc._snapshot(state, roster.labels, previous=written)
    assert carried == written
    assert snapshot_from_json(snapshot_to_json(written)) == written
    # No baseline for an entrant of a draft that had none.
    plain = svc._snapshot(state, roster.labels)
    assert all(e.prefill_index is None and e.origin is None for e in plain)
    assert 'prefill_index' not in snapshot_to_json(plain)[0]
    assert svc._prefill_baseline(_drafted(state, plain)) is None
    assert svc._prefill_baseline(_drafted(state, ())) is None


def test_untouched_prefill_is_recognised_and_a_swap_is_not():
    roster = _prefill_roster()
    state = _prefill_state()
    snapshot = svc._snapshot(state, roster.labels, prefill=roster)
    untouched = _drafted(state, snapshot)

    def decoded(seeding):
        return seed_code.decode_seed_code(seeding.seed_code).unwrap()

    assert svc._is_untouched_prefill(untouched, decoded(untouched))
    for touched_state in (
        domain.swap_slots(state, 0, 2),
        domain.redraw(state, 99),
    ):
        touched = _drafted(touched_state, snapshot)
        assert not svc._is_untouched_prefill(touched, decoded(touched))
    legacy = _drafted(state, svc._snapshot(state, roster.labels))
    assert not svc._is_untouched_prefill(legacy, decoded(legacy))


def _ffa_state(n, param):
    return domain.initial_state(
        SeedingFormat.FREE_FOR_ALL,
        param,
        [str(_pid(i)) for i in range(1, n + 1)],
        tier_count=2,
        draw_seed=1,
    )


def _ffa_roster(state):
    return svc._Roster(state.roster, {}, ())


# fmt: off
@pytest.mark.parametrize(
    ('fmt', 'n', 'param', 'minimum', 'expected'),
    [
        (SeedingFormat.FREE_FOR_ALL, 9, 8, 6, ('5, 4', 6)),
        (SeedingFormat.FREE_FOR_ALL, 9, 8, 4, None),
        (SeedingFormat.FREE_FOR_ALL, 9, 8, None, None),
        (SeedingFormat.ROUND_ROBIN, 9, 3, 6, None),
    ],
)
# fmt: on
def test_problems_flag_ffa_lobbies_below_the_minimum(
    fmt, n, param, minimum, expected
):
    state = (
        _ffa_state(n, param)
        if fmt is SeedingFormat.FREE_FOR_ALL
        else domain.initial_state(
            fmt,
            param,
            [str(_pid(i)) for i in range(1, n + 1)],
            tier_count=1,
            draw_seed=1,
        )
    )

    msgids, params = svc._problems(
        state, _ffa_roster(state), lobby_minimum=minimum
    )

    if expected is None:
        assert msgids == []
    else:
        sizes, floor = expected
        assert msgids == [svc.PROBLEM_LOBBY_BELOW_MIN]
        assert params == [{'sizes': sizes, 'minimum': floor}]


def test_problems_keep_a_lobby_of_one_as_a_small_group_problem():
    state = _ffa_state(3, 2)

    msgids, _ = svc._problems(state, _ffa_roster(state), lobby_minimum=2)

    assert msgids == [domain.PROBLEM_SMALL_GROUP]


# fmt: off
@pytest.mark.parametrize(
    ('reason', 'msgid'),
    [
        ('below_minimum', FFA_STALLS_MSGID),
        ('no_progress', FFA_NO_PROGRESS_MSGID),
    ],
    ids=['below_minimum', 'no_progress'],
)
# fmt: on
def test_problems_flag_a_stalling_single_track(reason, msgid):
    state = _ffa_state(16, 4)
    stall = FfaDeadEnd(
        reason=reason,
        round_number=2,
        count=9,
        lobby_sizes=(3, 3, 3),
        minimum=4,
    )

    msgids, params = svc._problems(
        state, _ffa_roster(state), stall=stall, cut=3
    )

    assert msgids == [msgid]
    assert params == [
        {'count': 9, 'round': 3, 'sizes': '3, 3, 3', 'minimum': 4, 'cut': 3}
    ]


# -------------------------------------------------------------------- #
# playoff fixes count from the separated prefill

_FFA_IDS = tuple(f'f{i:02d}' for i in range(16))


def _ffa_prefill():
    roster = svc._Roster(
        _FFA_IDS,
        {i: i for i in _FFA_IDS},
        (),
        {cid: ('leaderboard', i + 1) for i, cid in enumerate(_FFA_IDS)},
        prefill_order=_FFA_IDS,
    )
    state = svc._prefill_playoff_state(
        SeedingFormat.FREE_FOR_ALL, 4, _FFA_IDS, {}, draw_seed=7
    )
    return state, roster


def _prefilled_seeding(state, roster):
    snapshot = svc._snapshot(state, roster.labels, prefill=roster)
    return _drafted(state, snapshot)


_SEP_IDS = ('a', 'b', 'c', 'd')
_SEP_ORIGIN = {'a': 'group:0', 'b': 'group:1', 'c': 'group:1', 'd': 'group:0'}


def _separated_roster():
    origins = {
        cid: (_SEP_ORIGIN[cid], i + 1) for i, cid in enumerate(_SEP_IDS)
    }
    return svc._Roster(
        _SEP_IDS,
        {i: i for i in _SEP_IDS},
        (),
        origins,
        prefill_order=_SEP_IDS,
    )


def _separated_state():
    return svc._prefill_playoff_state(
        SeedingFormat.SINGLE_ELIMINATION,
        0,
        _SEP_IDS,
        _SEP_ORIGIN,
        draw_seed=7,
    )


def test_fresh_playoff_draft_counts_no_fixes():
    roster = _separated_roster()
    state = _separated_state()

    assert domain.fix_count(state) == 1
    assert svc._fix_count(state, roster) == 0
    assert svc._is_prefilled(state, _prefilled_seeding(state, roster))


def test_reset_on_a_playoff_draft_returns_to_the_separated_prefill():
    roster = _separated_roster()
    prefill = _separated_state()
    state = domain.swap_slots(prefill, 0, 2)
    assert svc._fix_count(state, roster) == 1

    reset = svc._apply(
        state,
        svc.ResetFixes(),
        roster,
        SeedingFormat.SINGLE_ELIMINATION,
        0,
    ).unwrap()

    assert reset == prefill
    assert svc._fix_count(reset, roster) == 0


def test_a_hand_swap_on_a_prefill_counts_one_fix():
    roster = _separated_roster()
    prefill = _separated_state()
    swapped = domain.swap_slots(prefill, 0, 2)

    assert svc._fix_count(swapped, roster) == 1
    # Undoing the separation is a fix, too.
    assert svc._fix_count(domain.reset_fixes(prefill), roster) == 1


def test_reset_keeps_the_chosen_tiers():
    state, roster = _ffa_prefill()
    assert state.tier_count == 4
    state = domain.set_tier_count(state, 2)
    swapped = domain.swap_slots(state, 0, 5)
    assert svc._fix_count(swapped, roster) == 1

    reset = svc._apply(
        swapped, svc.ResetFixes(), roster, SeedingFormat.FREE_FOR_ALL, 4
    ).unwrap()

    assert svc._fix_count(reset, roster) == 0
    assert (reset.tier_count, reset.tiers, reset.seed_list) == (
        state.tier_count,
        state.tiers,
        state.seed_list,
    )
    assert reset.draw_seed == state.draw_seed


def test_a_tier_boundary_move_is_not_prefilled():
    state, roster = _ffa_prefill()
    seeding = _prefilled_seeding(state, roster)
    assert svc._is_prefilled(state, seeding)
    tiers = dict(zip(state.roster, state.tiers, strict=True))
    last_of_first = [c for c in state.seed_list if tiers[c] == 0][-1]
    first_of_second = [c for c in state.seed_list if tiers[c] == 1][0]

    moved = domain.move_to_tier(
        state, last_of_first, 1, ref_id=first_of_second, after=False
    )

    assert moved.seed_list == state.seed_list
    assert moved.tiers != state.tiers
    assert not svc._is_prefilled(moved, seeding)
    resized = domain.set_tier_count(state, 2)
    assert not svc._is_prefilled(resized, seeding)


def _noop_setup(make_world, **kw):
    world = make_world(**kw)
    board = svc.get_board(TOURNAMENT_ID).unwrap()
    make_world.logs.reset_mock()
    make_world.repo.commit_session.reset_mock()
    return world, board


def _assert_nothing_written(world, make_world, board, result):
    assert result.unwrap().version == board.version
    assert result.unwrap().code == board.code
    stored = world.seedings[(TOURNAMENT_ID, 'initial')]
    assert stored.version == board.version
    make_world.logs.assert_not_called()


def test_noop_swap_writes_nothing(make_world):
    world, board = _noop_setup(make_world)

    result = svc.apply_action(
        TOURNAMENT_ID, 'initial', svc.Swap(3, 3),
        expected_version=board.version, initiator_id=INITIATOR,
    )  # fmt: skip

    _assert_nothing_written(world, make_world, board, result)


def test_noop_replay_of_the_current_code_writes_nothing(make_world):
    world, board = _noop_setup(make_world)

    result = svc.apply_action(
        TOURNAMENT_ID, 'initial', svc.Replay(board.code),
        expected_version=board.version, initiator_id=INITIATOR,
    )  # fmt: skip

    _assert_nothing_written(world, make_world, board, result)


def test_noop_reseed_on_an_unchanged_roster_writes_nothing(make_world):
    world, board = _noop_setup(make_world)

    result = svc.apply_action(
        TOURNAMENT_ID, 'initial', svc.ReseedKeepTiers(),
        expected_version=board.version, initiator_id=INITIATOR,
    )  # fmt: skip

    _assert_nothing_written(world, make_world, board, result)


def test_a_stale_draft_still_writes_on_reseed(make_world):
    world, board = _noop_setup(make_world)
    world.active.pop()

    result = svc.apply_action(
        TOURNAMENT_ID, 'initial', svc.ReseedKeepTiers(),
        expected_version=board.version, initiator_id=INITIATOR,
    ).unwrap()  # fmt: skip

    assert result.version == board.version + 1
    assert _event_types(make_world.logs) == ['seeding-roster-reseeded']


def _logged_data(make_world):
    return make_world.logs.call_args.kwargs['data']


def test_swap_logs_slots_occupants_and_units(make_world):
    _, board = _noop_setup(make_world)
    before = board.state.layout

    svc.apply_action(
        TOURNAMENT_ID, 'initial', svc.Swap(1, 6),
        expected_version=board.version, initiator_id=INITIATOR,
    ).unwrap()  # fmt: skip

    data = _logged_data(make_world)
    assert data['p'] == 1
    assert data['q'] == 6
    assert data['a'] == before[1]
    assert data['b'] == before[6]
    assert (data['unit_p'], data['unit_q']) == (0, 3)
    assert data['format'] == 'SINGLE_ELIMINATION'
    assert data['target'] == 'initial'
    assert data['version'] == board.version + 1


def test_swap_logs_none_for_a_bye_and_group_units():
    state = domain.initial_state(
        SeedingFormat.SINGLE_ELIMINATION,
        0,
        ('a', 'b', 'c'),
        tier_count=1,
        draw_seed=7,
    )
    bye = state.layout.index(None)
    other = 0 if bye != 0 else 1
    after = domain.swap_slots(state, bye, other)

    data = svc._event_data(
        svc.Swap(bye, other), state, after, svc._Roster((), {}, ())
    )

    assert data['a'] is None
    assert data['b'] == state.layout[other]
    assert (data['unit_p'], data['unit_q']) == (bye // 2, other // 2)

    rr = domain.initial_state(
        SeedingFormat.ROUND_ROBIN,
        2,
        tuple('abcdefg'),
        tier_count=1,
        draw_seed=7,
    )
    sizes = domain.group_sizes(rr.format, len(rr.layout), rr.param)
    assert sizes == [3, 4]
    data = svc._event_data(
        svc.Swap(2, 3), rr, domain.swap_slots(rr, 2, 3), svc._Roster((), {}, ())
    )
    assert (data['unit_p'], data['unit_q']) == (0, 1)
    assert data['format'] == 'ROUND_ROBIN'


def test_tier_move_logs_from_to_and_place(make_world):
    make_world(_tournament(FFA, group_size_max=4), n=8)
    board = svc.get_board(TOURNAMENT_ID).unwrap()
    tiered = svc.apply_action(
        TOURNAMENT_ID, 'initial', svc.SetTierCount(2),
        expected_version=board.version, initiator_id=INITIATOR,
    ).unwrap()  # fmt: skip
    cid = tiered.state.seed_list[0]

    moved = svc.apply_action(
        TOURNAMENT_ID, 'initial', svc.MoveTier(cid, 1),
        expected_version=tiered.version, initiator_id=INITIATOR,
    ).unwrap()  # fmt: skip

    data = _logged_data(make_world)
    tier_of = dict(zip(moved.state.roster, moved.state.tiers, strict=True))
    members = [c for c in moved.state.seed_list if tier_of[c] == 1]
    assert make_world.logs.call_args.args[0] == 'seeding-tier-changed'
    assert data['contestant_id'] == cid
    assert (data['from_tier'], data['to_tier']) == (0, 1)
    assert data['place_in_tier'] == members.index(cid) + 1 == len(members)


def test_tier_resize_logs_count_and_dropped_fixes(make_world):
    make_world(_tournament(FFA, group_size_max=4), n=8)
    board = svc.get_board(TOURNAMENT_ID).unwrap()
    swapped = svc.apply_action(
        TOURNAMENT_ID, 'initial', svc.Swap(0, 5),
        expected_version=board.version, initiator_id=INITIATOR,
    ).unwrap()  # fmt: skip
    assert swapped.fix_count == 1

    resized = svc.apply_action(
        TOURNAMENT_ID, 'initial', svc.SetTierCount(3),
        expected_version=swapped.version, initiator_id=INITIATOR,
    ).unwrap()  # fmt: skip

    data = _logged_data(make_world)
    assert resized.fix_count == 0
    assert data['tier_count'] == 3
    assert data['dropped_fixes'] == 1


def test_reset_logs_dropped_fixes(make_world):
    make_world()
    board = svc.get_board(TOURNAMENT_ID).unwrap()
    swapped = svc.apply_action(
        TOURNAMENT_ID, 'initial', svc.Swap(0, 5),
        expected_version=board.version, initiator_id=INITIATOR,
    ).unwrap()  # fmt: skip

    svc.apply_action(
        TOURNAMENT_ID, 'initial', svc.ResetFixes(),
        expected_version=swapped.version, initiator_id=INITIATOR,
    ).unwrap()  # fmt: skip

    assert _logged_data(make_world)['dropped_fixes'] == 1


def test_reseed_logs_leavers_and_joiners_as_strings(make_world):
    world, board = _noop_setup(make_world)
    leaver = world.active.pop()
    world.active.append(_pid(99))

    svc.apply_action(
        TOURNAMENT_ID, 'initial', svc.ReseedKeepTiers(),
        expected_version=board.version, initiator_id=INITIATOR,
    ).unwrap()  # fmt: skip

    data = _logged_data(make_world)
    assert data['leaver_ids'] == [str(leaver)]
    assert data['joiner_ids'] == [str(_pid(99))]


def test_undoing_the_separation_counts_and_logs_one_fix():
    roster = _separated_roster()
    prefill = _separated_state()
    undone = domain.reset_fixes(prefill)
    assert svc._fix_count(prefill, roster) == 0
    assert svc._fix_count(undone, roster) == 1

    reset = svc._apply(
        undone,
        svc.ResetFixes(),
        roster,
        SeedingFormat.SINGLE_ELIMINATION,
        0,
    ).unwrap()
    data = svc._event_data(svc.ResetFixes(), undone, reset, roster)

    assert svc._fix_count(reset, roster) == 0
    assert data == {'dropped_fixes': 1}
    # A reset can never log a negative count.
    data = svc._event_data(svc.ResetFixes(), prefill, undone, roster)
    assert data == {'dropped_fixes': 0}
