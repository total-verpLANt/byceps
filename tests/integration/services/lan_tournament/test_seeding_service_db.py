"""
tests.integration.services.lan_tournament.test_seeding_service_db
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from dataclasses import replace
from datetime import datetime, UTC
from itertools import count

import pytest

from byceps.database import db
from byceps.services.lan_tournament import (
    lan_tournament_view_helpers as helpers,
    tournament_log_service,
    tournament_match_service,
    tournament_participant_service,
    tournament_repository,
    tournament_seeding_repository,
    tournament_seeding_service as svc,
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
from byceps.util.result import Err
from byceps.util.uuid import generate_uuid7


PARTY_ID = PartyID('lan-party-2024-seeding-service')

_counter = count(1)


@pytest.fixture(scope='module')
def party(make_party, make_brand):
    brand = make_brand('seedingservicebrand', 'Seeding Service Brand')
    return make_party(brand, PARTY_ID, 'LAN Party 2024 Seeding Service')


@pytest.fixture(scope='module')
def users(make_user):
    return [make_user(f'SeedingServiceUser{i}') for i in range(16)]


@pytest.fixture
def make_tournament(party, users):
    created = []

    def _make(
        status=TournamentStatus.REGISTRATION_CLOSED,
        participants=8,
        mode=(GameFormat.ONE_V_ONE, EliminationMode.SINGLE_ELIMINATION),
        **kwargs,
    ):
        result = tournament_service.create_tournament(
            PARTY_ID,
            f'Seeding Service Tournament {next(_counter)}',
            contestant_type=ContestantType.SOLO,
            game_format=mode[0],
            elimination_mode=mode[1],
            tournament_status=status,
            **kwargs,
        )
        assert result.is_ok()
        tournament, _ = result.unwrap()
        created.append(tournament)
        ids = []
        for user in users[:participants]:
            pid = TournamentParticipantID(generate_uuid7())
            tournament_repository.create_participant(
                TournamentParticipant(
                    id=pid,
                    user_id=user.id,
                    tournament_id=tournament.id,
                    substitute_player=False,
                    team_id=None,
                    created_at=datetime.now(UTC),
                )
            )
            ids.append(pid)
        db.session.commit()
        return tournament, ids

    yield _make
    db.session.rollback()
    for tournament in created:
        if tournament_repository.find_tournament(tournament.id) is not None:
            tournament_service.delete_tournament(tournament.id)


def _stored(tournament):
    db.session.rollback()
    return tournament_seeding_repository.find_seeding(tournament.id, 'initial')


def test_first_open_persists_draw(make_tournament):
    tournament, _ = make_tournament()
    assert _stored(tournament) is None

    board = svc.get_board(tournament.id).unwrap()

    stored = _stored(tournament)
    assert stored is not None
    assert stored.version == 1
    assert stored.generated_seed_code is None
    assert board.version == 1
    assert board.code == svc.seed_code.format_seed_code(stored.seed_code)
    assert board.pure_draw
    assert board.problems == ()
    events = [
        e.event_type
        for e in tournament_log_service.get_entries_for_tournament(
            tournament.id
        )
    ]
    assert events.count('seeding-drawn') == 1


def test_reload_returns_same_code(make_tournament):
    tournament, _ = make_tournament()

    first = svc.get_board(tournament.id).unwrap()
    second = svc.get_board(tournament.id).unwrap()

    assert second.code == first.code
    assert second.state == first.state
    assert second.version == first.version
    db.session.rollback()
    assert (
        len(
            tournament_seeding_repository.get_seedings_for_tournament(
                tournament.id
            )
        )
        == 1
    )


def test_first_open_before_registration_closed_persists_nothing(
    make_tournament,
):
    tournament, _ = make_tournament(TournamentStatus.REGISTRATION_OPEN)

    result = svc.get_board(tournament.id)

    assert result.is_err()
    assert _stored(tournament) is None


def test_apply_action_is_durable_and_versioned(make_tournament, users):
    tournament, _ = make_tournament()
    board = svc.get_board(tournament.id).unwrap()

    new = svc.apply_action(
        tournament.id,
        'initial',
        svc.Swap(0, 1),
        expected_version=board.version,
        initiator_id=users[0].id,
    ).unwrap()

    stored = _stored(tournament)
    assert stored.version == 2
    assert stored.updated_by == users[0].id
    assert new.code == svc.seed_code.format_seed_code(stored.seed_code)
    events = [
        e.event_type
        for e in tournament_log_service.get_entries_for_tournament(
            tournament.id
        )
    ]
    assert 'seeding-swapped' in events

    conflict = svc.apply_action(
        tournament.id,
        'initial',
        svc.Swap(2, 3),
        expected_version=board.version,
        initiator_id=users[0].id,
    )
    assert conflict.is_err()
    assert _stored(tournament).version == 2


def test_soft_removed_participant_makes_board_stale(make_tournament, users):
    tournament, ids = make_tournament()
    board = svc.get_board(tournament.id).unwrap()

    tournament_repository.soft_delete_participants_by_ids(
        {ids[3]}, datetime.now(UTC)
    )
    db.session.commit()

    stale = svc.get_board(tournament.id).unwrap()
    assert stale.stale
    assert stale.stale_leaver_ids == (str(ids[3]),)
    assert stale.stale_leavers == (users[3].screen_name,)
    assert stale.state.roster == board.state.roster

    fresh = svc.apply_action(
        tournament.id,
        'initial',
        svc.ReseedKeepTiers(),
        expected_version=stale.version,
        initiator_id=users[0].id,
    ).unwrap()
    assert not fresh.stale
    assert str(ids[3]) not in fresh.state.roster
    assert len(fresh.state.roster) == 7


def test_delete_tournament_deletes_seedings(make_tournament):
    tournament, _ = make_tournament()
    svc.get_board(tournament.id).unwrap()
    assert _stored(tournament) is not None

    tournament_service.delete_tournament(tournament.id)

    db.session.rollback()
    assert (
        tournament_seeding_repository.get_seedings_for_tournament(tournament.id)
        == []
    )
    assert tournament_repository.find_tournament(tournament.id) is None


def test_stale_after_hard_removal_keeps_tiers_and_names_leaver(
    make_tournament, users
):
    tournament, ids = make_tournament(
        mode=(GameFormat.FREE_FOR_ALL, EliminationMode.SINGLE_ELIMINATION),
        group_size_max=4,
        point_table=[3, 2, 1],
        advancement_count=1,
    )
    board = svc.get_board(tournament.id).unwrap()
    tiered = svc.apply_action(
        tournament.id,
        'initial',
        svc.SetTierCount(3),
        expected_version=board.version,
        initiator_id=users[0].id,
    ).unwrap()
    tier_before = dict(
        zip(tiered.state.roster, tiered.state.tiers, strict=True)
    )

    removed = tournament_participant_service.admin_remove_participant(
        tournament.id, ids[3]
    )
    assert removed.is_ok()
    db.session.rollback()
    assert tournament_repository.find_participant(ids[3]) is None
    assert ids[3] not in [
        p.id
        for p in tournament_repository.get_participants_for_tournament(
            tournament.id, include_removed=True
        )
    ]

    stale = svc.get_board(tournament.id).unwrap()
    assert stale.stale
    assert stale.stale_leavers == (users[3].screen_name,)
    assert stale.stale_leaver_ids == (str(ids[3]),)
    assert stale.labels[str(ids[3])] == users[3].screen_name
    assert stale.state == tiered.state

    fresh = svc.apply_action(
        tournament.id,
        'initial',
        svc.ReseedKeepTiers(),
        expected_version=stale.version,
        initiator_id=users[0].id,
    ).unwrap()

    assert not fresh.stale
    tier_after = dict(zip(fresh.state.roster, fresh.state.tiers, strict=True))
    assert tier_after == {
        cid: t for cid, t in tier_before.items() if cid != str(ids[3])
    }
    stored = _stored(tournament)
    assert [e.id for e in stored.roster_snapshot] == list(fresh.state.roster)
    assert str(ids[3]) not in [e.id for e in stored.roster_snapshot]
    events = [
        e.event_type
        for e in tournament_log_service.get_entries_for_tournament(
            tournament.id
        )
    ]
    assert 'seeding-roster-reseeded' in events


def test_reseed_marks_joiners_new_until_the_next_generation(
    make_tournament, users
):
    tournament, _ = make_tournament(participants=7)
    board = svc.get_board(tournament.id).unwrap()
    assert board.new_entrant_ids == ()
    joiner = TournamentParticipantID(generate_uuid7())
    tournament_repository.create_participant(
        TournamentParticipant(
            id=joiner,
            user_id=users[7].id,
            tournament_id=tournament.id,
            substitute_player=False,
            team_id=None,
            created_at=datetime.now(UTC),
        )
    )
    db.session.commit()
    stale = svc.get_board(tournament.id).unwrap()
    assert stale.stale_joiner_ids == (str(joiner),)
    assert stale.new_entrant_ids == ()

    fresh = svc.apply_action(
        tournament.id,
        'initial',
        svc.ReseedKeepTiers(),
        expected_version=stale.version,
        initiator_id=users[0].id,
    ).unwrap()
    db.session.commit()

    assert fresh.new_entrant_ids == (str(joiner),)
    assert svc.get_board(tournament.id).unwrap().new_entrant_ids == (
        str(joiner),
    )
    stored = _stored(tournament)
    assert [e.id for e in stored.roster_snapshot if e.joined_late] == [
        str(joiner)
    ]

    generated = svc.generate_from_seeding(
        tournament.id,
        expected_version=fresh.version,
        initiator_id=users[0].id,
    )
    assert generated.is_ok()
    db.session.commit()
    assert svc.get_board(tournament.id).unwrap().new_entrant_ids == ()
    assert not any(e.joined_late for e in _stored(tournament).roster_snapshot)


@pytest.fixture
def make_playoff(party, users):
    """Return a factory for a 2-group round robin whose groups are played."""
    created = []

    def _make():
        result = tournament_service.create_tournament(
            PARTY_ID,
            f'Seeding Service Playoff {next(_counter)}',
            contestant_type=ContestantType.SOLO,
            game_format=GameFormat.ONE_V_ONE,
            elimination_mode=EliminationMode.ROUND_ROBIN,
            tournament_status=TournamentStatus.REGISTRATION_CLOSED,
            playoff_game_format=GameFormat.ONE_V_ONE,
            playoff_elimination_mode=EliminationMode.SINGLE_ELIMINATION,
            playoff_group_count=2,
            playoff_qualifiers_per_group=2,
            playoff_release_mode=PlayoffReleaseMode.MANUAL,
        )
        assert result.is_ok(), result.unwrap_err()
        tournament, _ = result.unwrap()
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
        generated = tournament_match_service.generate_round_robin_bracket(
            tournament.id
        )
        assert generated.is_ok(), generated.unwrap_err()
        started = tournament_service.change_status(
            tournament.id, TournamentStatus.ONGOING, users[0].id
        )
        assert started.is_ok(), started.unwrap_err()
        matches = tournament_repository.get_matches_for_tournament(
            tournament.id
        )
        contestants = tournament_repository.get_contestants_for_tournament(
            tournament.id
        )
        for match in matches:
            ids = [c.participant_id for c in contestants[match.id]]
            low = min(ids, key=str)
            margin = match.group_order + 1
            scores = {i: (margin if i == low else 0) for i in ids}
            confirmed = tournament_match_service.admin_set_and_confirm_match(
                match.id, users[0].id, scores
            )
            assert confirmed.is_ok(), confirmed.unwrap_err()
        board = svc.ensure_playoff_draft(tournament.id)
        assert board.is_ok(), board.unwrap_err()
        return tournament, board.unwrap()

    yield _make
    db.session.rollback()
    for tournament in created:
        if tournament_repository.find_tournament(tournament.id) is not None:
            tournament_service.delete_tournament(tournament.id)


def _swap_into_one_group(tournament, board, users):
    """Put the two qualifiers of group A into first-round match 1."""
    by_label = {label: cid for cid, label in board.origin_labels.items()}
    layout = list(board.state.layout)
    first = layout.index(by_label['A1'])
    second = layout.index(by_label['A2'])
    partner = first ^ 1
    swapped = svc.apply_action(
        tournament.id,
        'playoff',
        svc.Swap(second, partner),
        expected_version=board.version,
        initiator_id=users[0].id,
    )
    assert swapped.is_ok(), swapped.unwrap_err()
    return swapped.unwrap()


def _events(tournament):
    db.session.rollback()
    return [
        e.event_type
        for e in tournament_log_service.get_entries_for_tournament(
            tournament.id
        )
    ]


@pytest.fixture
def plain_translations(monkeypatch):
    def translate(message, **params):
        return message % params if params else message

    def ntranslate(singular, plural, num, **params):
        params.setdefault('num', num)
        return (singular if num == 1 else plural) % params

    monkeypatch.setattr(helpers, 'gettext', translate)
    monkeypatch.setattr(helpers, 'ngettext', ntranslate)


def test_playoff_board_payload_has_origins_and_same_group_flags(
    make_playoff, users, plain_translations
):
    tournament, board = make_playoff()

    assert sorted(board.origin_labels.values()) == ['A1', 'A2', 'B1', 'B2']
    assert board.same_group_matches == ()
    payload = helpers.seeding_board_payload(board)
    slots = [s for m in payload['layout']['matches'] for s in m['slots']]
    assert {s['contestant_id']: s['origin'] for s in slots} == (
        board.origin_labels
    )
    assert [m['same_group'] for m in payload['layout']['matches']] == [
        False,
        False,
    ]

    clashing = _swap_into_one_group(tournament, board, users)

    assert len(clashing.same_group_matches) >= 1
    payload = helpers.seeding_board_payload(clashing)
    flagged = [
        m['number'] for m in payload['layout']['matches'] if m['same_group']
    ]
    assert flagged == list(clashing.same_group_matches)
    for match in payload['layout']['matches']:
        info = match['same_group_info']
        if not match['same_group']:
            assert info is None
            continue
        a, b = info['a']['origin'], info['b']['origin']
        assert info['group'] == a[0] == b[0]
        assert {a[1], b[1]} == {'1', '2'}


def test_initial_board_has_no_origins(make_tournament, plain_translations):
    tournament, _ = make_tournament()
    board = svc.get_board(tournament.id).unwrap()

    assert board.origin_labels == {}
    assert board.same_group_matches == ()
    payload = helpers.seeding_board_payload(board)
    slots = [s for m in payload['layout']['matches'] for s in m['slots']]
    assert all(s['origin'] is None for s in slots)
    assert not any(m['same_group'] for m in payload['layout']['matches'])


def test_separate_action_splits_same_group_pairing(make_playoff, users):
    tournament, board = make_playoff()
    clashing = _swap_into_one_group(tournament, board, users)
    db.session.commit()
    assert clashing.same_group_matches

    result = svc.apply_action(
        tournament.id,
        'playoff',
        svc.Separate(),
        expected_version=clashing.version,
        initiator_id=users[0].id,
    )

    assert result.is_ok(), result.unwrap_err()
    separated = result.unwrap()
    assert separated.same_group_matches == ()
    assert separated.version == clashing.version + 1
    assert separated.code != clashing.code
    events = _events(tournament)
    assert events.count('seeding-separated') == 1
    stored = tournament_seeding_repository.find_seeding(
        tournament.id, 'playoff'
    )
    assert stored.version == separated.version


def test_separate_action_refuses_a_stale_version(make_playoff, users):
    tournament, board = make_playoff()
    clashing = _swap_into_one_group(tournament, board, users)
    db.session.commit()

    result = svc.apply_action(
        tournament.id,
        'playoff',
        svc.Separate(),
        expected_version=clashing.version - 1,
        initiator_id=users[0].id,
    )

    assert result.is_err()
    assert result.unwrap_err() == svc.ERR_CONFLICT
    assert 'seeding-separated' not in _events(tournament)
    assert svc.get_board(tournament.id, 'playoff').unwrap().version == (
        clashing.version
    )


def test_separate_action_refused_when_nothing_to_separate(make_playoff, users):
    tournament, board = make_playoff()
    assert board.same_group_matches == ()

    result = svc.apply_action(
        tournament.id,
        'playoff',
        svc.Separate(),
        expected_version=board.version,
        initiator_id=users[0].id,
    )

    assert result.is_err()
    assert result.unwrap_err() == svc.ERR_NOTHING_TO_SEPARATE
    assert 'seeding-separated' not in _events(tournament)
    assert svc.get_board(tournament.id, 'playoff').unwrap().version == (
        board.version
    )


def test_separate_action_refused_on_the_initial_target(make_tournament, users):
    tournament, _ = make_tournament()
    board = svc.get_board(tournament.id).unwrap()

    result = svc.apply_action(
        tournament.id,
        'initial',
        svc.Separate(),
        expected_version=board.version,
        initiator_id=users[0].id,
    )

    assert result.is_err()
    assert result.unwrap_err() == svc.ERR_INVALID_CHANGE
    assert 'seeding-separated' not in _events(tournament)


FFA_SE = (GameFormat.FREE_FOR_ALL, EliminationMode.SINGLE_ELIMINATION)


def _ffa_board(tournament, target='initial'):
    return svc.get_board(tournament.id, target).unwrap()


def test_ffa_board_flags_lobbies_below_the_minimum(make_tournament, users):
    tournament, _ = make_tournament(
        participants=9,
        mode=FFA_SE,
        group_size_min=6,
        group_size_max=8,
        advancement_count=2,
        point_table=[10, 6, 3, 1],
        max_players=16,
    )

    board = _ffa_board(tournament)

    assert svc.PROBLEM_LOBBY_BELOW_MIN in board.problems
    params = board.problem_params[
        board.problems.index(svc.PROBLEM_LOBBY_BELOW_MIN)
    ]
    assert params == {'sizes': '5, 4', 'minimum': 6}
    result = svc.generate_from_seeding(
        tournament.id,
        expected_version=board.version,
        initiator_id=users[0].id,
    )
    assert result == Err(svc.ERR_PROBLEMS)
    assert tournament_repository.get_matches_for_tournament(tournament.id) == []


def test_a_stalling_initial_board_names_the_round(make_tournament, users):
    tournament, _ = make_tournament(
        participants=16,
        mode=FFA_SE,
        group_size_min=4,
        group_size_max=4,
        advancement_count=3,
        point_table=[10, 6, 3, 1],
        max_players=16,
    )

    board = _ffa_board(tournament)

    assert svc.PROBLEM_FFA_STALLS in board.problems
    params = board.problem_params[board.problems.index(svc.PROBLEM_FFA_STALLS)]
    assert params == {
        'count': 9,
        'round': 3,
        'sizes': '3, 3, 3',
        'minimum': 4,
        'cut': 3,
    }
    result = svc.generate_from_seeding(
        tournament.id,
        expected_version=board.version,
        initiator_id=users[0].id,
    )
    assert result == Err(svc.ERR_PROBLEMS)
    assert tournament_repository.get_matches_for_tournament(tournament.id) == []


def test_a_fresh_stalling_ffa_is_refused_at_round_one(make_tournament, users):
    """`16 -> 12 -> 9 -> [3, 3, 3]` never gets a round one any more."""
    tournament, _ = make_tournament(
        participants=16,
        mode=FFA_SE,
        group_size_min=4,
        group_size_max=4,
        advancement_count=3,
        point_table=[10, 6, 3, 1],
        max_players=16,
    )

    result = tournament_match_service.generate_ffa_round(
        tournament.id, initiator_id=users[0].id
    )

    assert result == Err(tournament_match_service.FFA_STALLS_ERROR)


def _play_round(tournament, round_number, user):
    for match in tournament_repository.get_matches_for_round(
        tournament.id, round_number
    ):
        members = [
            str(c.participant_id)
            for c in tournament_match_service.get_contestants_for_match(
                match.id
            )
        ]
        placed = tournament_match_service.set_ffa_placements(
            match.id, {cid: i + 1 for i, cid in enumerate(members)}
        )
        assert placed.is_ok(), placed.unwrap_err()
        confirmed = tournament_match_service.confirm_ffa_match(match.id, user.id)
        assert confirmed.is_ok(), confirmed.unwrap_err()


@pytest.fixture
def legacy_stalling_ffa(make_tournament, users):
    """Return a running 16/4/4 FFA whose cut stalls round two.

    A pre-remediation-C state: the service refuses 16/4/4/3 at round one
    now, so the cut is written straight through the repository after the
    start, bypassing the service.
    """
    tournament, _ = make_tournament(
        participants=16,
        mode=FFA_SE,
        group_size_min=4,
        group_size_max=4,
        advancement_count=2,
        point_table=[10, 6, 3, 1],
        max_players=16,
    )
    board = _ffa_board(tournament)
    assert board.problems == ()
    svc.generate_from_seeding(
        tournament.id,
        expected_version=board.version,
        initiator_id=users[0].id,
    ).unwrap()
    tournament_service.change_status(
        tournament.id, TournamentStatus.ONGOING, users[0].id
    ).unwrap()
    tournament_repository.update_tournament(
        replace(
            tournament_repository.get_tournament(tournament.id),
            advancement_count=3,
        )
    )
    db.session.commit()
    return tournament


def test_an_unpermitted_ffa_round_draft_names_the_problem(
    legacy_stalling_ffa, users
):
    tournament = legacy_stalling_ffa
    _play_round(tournament, 0, users[0])
    target = svc.prepare_ffa_round_draft(
        tournament.id, initiator_id=users[0].id
    ).unwrap()
    board = _ffa_board(tournament, target)
    svc.generate_from_seeding(
        tournament.id,
        target,
        expected_version=board.version,
        initiator_id=users[0].id,
    ).unwrap()
    _play_round(tournament, 1, users[0])
    target = svc.prepare_ffa_round_draft(
        tournament.id, initiator_id=users[0].id
    ).unwrap()

    board = _ffa_board(tournament, target)

    assert len(board.state.roster) == 9
    assert svc._group_sizes(board.state) == [3, 3, 3]
    assert svc.PROBLEM_LOBBY_BELOW_MIN in board.problems
    params = board.problem_params[
        board.problems.index(svc.PROBLEM_LOBBY_BELOW_MIN)
    ]
    assert params == {'sizes': '3, 3, 3', 'minimum': 4}
    result = svc.generate_from_seeding(
        tournament.id,
        target,
        expected_version=board.version,
        initiator_id=users[0].id,
    )
    assert result == Err(svc.ERR_PROBLEMS)


def test_noop_action_keeps_version_and_audit(make_tournament, users):
    tournament, _ = make_tournament()
    board = svc.get_board(tournament.id).unwrap()
    events = _events(tournament)

    for action in (
        svc.Swap(2, 2),
        svc.Replay(board.code),
        svc.ReseedKeepTiers(),
    ):
        result = svc.apply_action(
            tournament.id,
            'initial',
            action,
            expected_version=board.version,
            initiator_id=users[0].id,
        )
        assert result.is_ok(), result.unwrap_err()
        assert result.unwrap().version == board.version
        assert result.unwrap().code == board.code

    assert _stored(tournament).version == board.version
    assert _events(tournament) == events


def test_swap_audit_row_carries_the_designed_keys(make_tournament, users):
    tournament, _ = make_tournament()
    board = svc.get_board(tournament.id).unwrap()

    svc.apply_action(
        tournament.id,
        'initial',
        svc.Swap(0, 5),
        expected_version=board.version,
        initiator_id=users[0].id,
    ).unwrap()

    db.session.rollback()
    entry = [
        e
        for e in tournament_log_service.get_entries_for_tournament(
            tournament.id
        )
        if e.event_type == 'seeding-swapped'
    ][0]
    assert entry.data['p'] == 0
    assert entry.data['q'] == 5
    assert entry.data['a'] == board.state.layout[0]
    assert entry.data['b'] == board.state.layout[5]
    assert (entry.data['unit_p'], entry.data['unit_q']) == (0, 2)
    assert entry.data['format'] == 'SINGLE_ELIMINATION'
