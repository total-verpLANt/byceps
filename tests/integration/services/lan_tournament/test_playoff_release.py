"""
tests.integration.services.lan_tournament.test_playoff_release
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from dataclasses import replace
from datetime import datetime, UTC
from itertools import count

import pytest
from sqlalchemy import select, text

from byceps.database import db
from byceps.services.lan_tournament import (
    signals,
    tournament_match_service,
    tournament_qualification_domain_service as qualification_domain,
    tournament_qualification_service,
    tournament_repository,
    tournament_score_service,
    tournament_seeding_domain_service as seeding_domain,
    tournament_seeding_repository,
    tournament_seeding_service,
    tournament_service,
)
from byceps.services.lan_tournament.dbmodels.tournament_log_entry import (
    DbTournamentLogEntry,
)
from byceps.services.lan_tournament.models import ContestantType
from byceps.services.lan_tournament.models.bracket import Bracket
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.playoff import PlayoffReleaseMode
from byceps.services.lan_tournament.models.score_ordering import ScoreOrdering
from byceps.services.lan_tournament.models.seeding import SeedingFormat
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


PARTY_ID = PartyID('lan-party-2024-playoff-release')

_counter = count(1)


@pytest.fixture(scope='module')
def party(make_party, make_brand):
    brand = make_brand('playoffreleasebrand', 'Playoff Release Brand')
    return make_party(brand, PARTY_ID, 'LAN Party 2024 Playoff Release')


@pytest.fixture(scope='module')
def users(make_user):
    return [make_user(f'PlayoffReleaseUser{i}') for i in range(8)]


@pytest.fixture
def make_tournament(party, users):
    created = []

    def _make(
        *,
        release_mode=PlayoffReleaseMode.MANUAL,
        playoff_mode=EliminationMode.SINGLE_ELIMINATION,
        participants=8,
        groups=2,
        qualifiers_per_group=2,
    ):
        result = tournament_service.create_tournament(
            PARTY_ID,
            f'Playoff Release Tournament {next(_counter)}',
            contestant_type=ContestantType.SOLO,
            game_format=GameFormat.ONE_V_ONE,
            elimination_mode=EliminationMode.ROUND_ROBIN,
            tournament_status=TournamentStatus.REGISTRATION_CLOSED,
            playoff_game_format=GameFormat.ONE_V_ONE,
            playoff_elimination_mode=playoff_mode,
            playoff_group_count=groups,
            playoff_qualifiers_per_group=qualifiers_per_group,
            playoff_release_mode=release_mode,
        )
        assert result.is_ok(), result.unwrap_err()
        tournament, _ = result.unwrap()
        created.append(tournament)
        for user in users[:participants]:
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
        return tournament

    yield _make
    db.session.rollback()
    for tournament in created:
        if tournament_repository.find_tournament(tournament.id) is not None:
            tournament_service.delete_tournament(tournament.id)


@pytest.fixture
def ready_events():
    events = []

    def receiver(sender, *, event):
        events.append(event)

    signals.match_ready.connect(receiver, weak=False)
    yield events
    signals.match_ready.disconnect(receiver)


def _groups(tournament):
    """Return the matches of each group, with their contestant ids."""
    matches = tournament_repository.get_matches_for_tournament(tournament.id)
    contestants = tournament_repository.get_contestants_for_tournament(
        tournament.id
    )
    groups: dict[int, list] = {}
    for match in matches:
        if match.phase != 1:
            continue
        ids = [c.participant_id for c in contestants[match.id]]
        groups.setdefault(match.group_order, []).append((match, ids))
    return groups


def _confirm(match, ids, scores, admin):
    result = tournament_match_service.admin_set_and_confirm_match(
        match.id, admin.id, dict(zip(ids, scores, strict=True))
    )
    assert result.is_ok(), result.unwrap_err()


def _play_groups(tournament, admin, *, skip_last=False):
    """Play every group match; the lower ID always wins.

    The margin is wide for the winner of group 0 and narrow elsewhere,
    so group 0 holds the best winner and the worst runner-up. That
    pairs them in the first playoff round, unless the draft separates
    them. With `skip_last`, return the match left open.
    """
    plan = []
    for group, matches in _groups(tournament).items():
        members = sorted({i for _, ids in matches for i in ids}, key=str)
        for match, ids in matches:
            low, high = sorted(ids, key=str)
            margin = 10 if group == 0 and members.index(low) == 0 else 1
            scores = (margin, 0) if ids[0] == low else (0, margin)
            plan.append((match, ids, scores))
    open_match = plan.pop() if skip_last else None
    for match, ids, scores in plan:
        _confirm(match, ids, scores, admin)
    return open_match


def _phase_two(tournament):
    return [
        m
        for m in tournament_repository.get_matches_for_tournament(tournament.id)
        if m.phase == 2
    ]


def _log_entries(tournament, event_type):
    with db.session.no_autoflush:
        return db.session.scalars(
            select(DbTournamentLogEntry).filter_by(
                tournament_id=tournament.id, event_type=event_type
            )
        ).all()


def _committed_release(tournament):
    """Read the release fields over a connection of their own."""
    with db.engine.connect() as connection:
        return connection.execute(
            text(
                'SELECT playoff_released_at, playoff_released_by,'
                ' playoff_auto_release_suspended'
                ' FROM lan_tournaments WHERE id = :id'
            ),
            {'id': tournament.id},
        ).one()


_LOG_COUNT_SQL = (
    'SELECT count(*) FROM lan_tournament_log_entries WHERE tournament_id = :id'
)


def _committed_count(sql, tournament):
    with db.engine.connect() as connection:
        return connection.execute(text(sql), {'id': tournament.id}).scalar()


def _qualification(tournament):
    return tournament_qualification_service.get_qualification(
        tournament.id
    ).unwrap()


def _release(tournament, admin):
    board = tournament_seeding_service.ensure_playoff_draft(
        tournament.id
    ).unwrap()
    return tournament_qualification_service.release_playoffs(
        tournament.id, expected_version=board.version, initiator_id=admin.id
    )


def test_manual_release_generates_phase_two(make_tournament, users):
    tournament = make_tournament()
    _play_groups(tournament, users[0])
    state = _qualification(tournament)
    assert state.ready and not state.released_at
    origin = {q.contestant_id: q.scope for q in state.qualifiers}

    board = tournament_seeding_service.ensure_playoff_draft(
        tournament.id
    ).unwrap()
    assert board.target == 'playoff'
    assert set(board.state.roster) == set(origin)
    assert (
        board.generation
        is tournament_seeding_service.GenerationStatus.NOT_GENERATED
    )

    unseparated = seeding_domain.derive_layout(
        SeedingFormat.SINGLE_ELIMINATION,
        [q.contestant_id for q in state.seed_order],
        0,
    )
    assert qualification_domain.same_group_matches(unseparated, origin), (
        'the setup must pair one group in round 1 before the separation'
    )
    assert not qualification_domain.same_group_matches(
        board.state.layout, origin
    )

    result = tournament_qualification_service.release_playoffs(
        tournament.id, expected_version=board.version, initiator_id=users[1].id
    )

    assert result.is_ok(), result.unwrap_err()
    assert result.unwrap() == 4
    phase_two = _phase_two(tournament)
    assert len(phase_two) == 4
    assert len(_groups(tournament)) == 2
    contestants = tournament_repository.get_contestants_for_tournament(
        tournament.id
    )
    first_round = [m for m in phase_two if m.round == 0]
    assert len(first_round) == 2
    placed = set()
    for match in first_round:
        ids = [str(c.participant_id) for c in contestants[match.id]]
        assert len(ids) == 2
        assert origin[ids[0]] != origin[ids[1]]
        placed.update(ids)
    assert placed == set(origin)

    released = tournament_repository.get_tournament(tournament.id)
    assert released.playoff_released_at is not None
    assert released.playoff_released_by == users[1].id
    assert _qualification(tournament).can_unrelease

    seeding = tournament_seeding_repository.find_seeding(
        tournament.id, 'playoff'
    )
    assert seeding.generated_seed_code == seeding.seed_code
    (entry,) = _log_entries(tournament, 'playoffs-released')
    assert entry.initiator_id == users[1].id
    assert entry.data['mode'] == 'manual'
    assert entry.data['match_count'] == 4
    assert len(_log_entries(tournament, 'bracket-generated')) == 1


def test_manual_release_needs_a_ready_qualification(make_tournament, users):
    tournament = make_tournament()
    _play_groups(tournament, users[0], skip_last=True)

    result = tournament_qualification_service.release_playoffs(
        tournament.id, expected_version=1, initiator_id=users[0].id
    )

    assert result.unwrap_err() == 'The qualification is not ready yet.'
    assert not _phase_two(tournament)
    assert _committed_release(tournament).playoff_released_at is None


def test_release_refuses_a_stale_draft_version(make_tournament, users):
    tournament = make_tournament()
    _play_groups(tournament, users[0])
    board = tournament_seeding_service.ensure_playoff_draft(
        tournament.id
    ).unwrap()

    result = tournament_qualification_service.release_playoffs(
        tournament.id,
        expected_version=board.version + 1,
        initiator_id=users[0].id,
    )

    assert result.unwrap_err() == tournament_seeding_service.ERR_CONFLICT
    assert not _phase_two(tournament)
    assert _committed_release(tournament).playoff_released_at is None


def test_release_twice_is_refused(make_tournament, users):
    tournament = make_tournament()
    _play_groups(tournament, users[0])
    assert _release(tournament, users[0]).is_ok()

    again = tournament_qualification_service.release_playoffs(
        tournament.id, expected_version=1, initiator_id=users[0].id
    )

    assert again.unwrap_err() == 'The playoffs are already released.'
    assert len(_phase_two(tournament)) == 4


def test_auto_release_on_last_result(make_tournament, users):
    tournament = make_tournament(release_mode=PlayoffReleaseMode.AUTOMATIC)
    open_match = _play_groups(tournament, users[0], skip_last=True)
    match, ids, scores = open_match
    assert not _phase_two(tournament)
    assert _committed_release(tournament).playoff_released_at is None

    _confirm(match, ids, scores, users[0])

    released = tournament_repository.get_tournament(tournament.id)
    assert released.playoff_released_at is not None
    assert released.playoff_released_by is None
    assert len(_phase_two(tournament)) == 4
    (entry,) = _log_entries(tournament, 'playoffs-released')
    assert entry.initiator_id is None
    assert entry.data['mode'] == 'automatic'
    # The system release has a user confirm the byes; none are needed here.
    assert len(_log_entries(tournament, 'bracket-generated')) == 1


def test_auto_release_is_idempotent(make_tournament, users):
    tournament = make_tournament(release_mode=PlayoffReleaseMode.AUTOMATIC)
    _play_groups(tournament, users[0])
    assert len(_phase_two(tournament)) == 4

    again = tournament_qualification_service.try_auto_release(
        tournament.id, triggered_by=users[0].id
    )

    assert again.unwrap() is False
    assert len(_phase_two(tournament)) == 4
    assert len(_log_entries(tournament, 'playoffs-released')) == 1


def test_manual_mode_never_releases_by_itself(make_tournament, users):
    tournament = make_tournament(release_mode=PlayoffReleaseMode.MANUAL)
    _play_groups(tournament, users[0])

    assert _qualification(tournament).ready
    assert not _phase_two(tournament)
    assert (
        tournament_qualification_service.try_auto_release(
            tournament.id, triggered_by=users[0].id
        ).unwrap()
        is False
    )


def test_auto_release_after_tie_decision(make_tournament, users):
    tournament = make_tournament(
        release_mode=PlayoffReleaseMode.AUTOMATIC,
        participants=4,
        qualifiers_per_group=1,
    )
    for matches in _groups(tournament).values():
        for match, ids in matches:
            _confirm(match, ids, (1, 1), users[0])
    blockers = _qualification(tournament).blockers
    assert {b.scope for b in blockers} == {'group:0', 'group:1'}

    first, second = blockers
    tournament_qualification_service.save_decision(
        tournament.id,
        first.scope,
        list(first.contestant_ids),
        reason='coin toss by the orga',
        initiator_id=users[0].id,
    ).unwrap()
    assert not _phase_two(tournament)

    tournament_qualification_service.save_decision(
        tournament.id,
        second.scope,
        list(second.contestant_ids),
        reason='coin toss by the orga',
        initiator_id=users[0].id,
    ).unwrap()

    assert len(_phase_two(tournament)) == 1
    assert _committed_release(tournament).playoff_released_at is not None


def test_automatic_release_waits_for_a_crossover_decision(
    make_tournament, users
):
    tournament = make_tournament(
        release_mode=PlayoffReleaseMode.AUTOMATIC,
        participants=4,
        qualifiers_per_group=2,
    )
    for matches in _groups(tournament).values():
        for match, ids in matches:
            low, _ = sorted(ids, key=str)
            _confirm(match, ids, (1, 0) if ids[0] == low else (0, 1), users[0])

    state = _qualification(tournament)

    assert not state.ready
    assert {b.scope for b in state.blockers} == {'crossover'}
    assert not _phase_two(tournament)
    assert _committed_release(tournament).playoff_released_at is None
    first, second = state.blockers
    tournament_qualification_service.save_decision(
        tournament.id,
        'crossover',
        list(first.contestant_ids),
        reason='seeded by the orga',
        initiator_id=users[0].id,
    ).unwrap()
    assert not _phase_two(tournament)

    tournament_qualification_service.save_decision(
        tournament.id,
        'crossover',
        list(second.contestant_ids),
        reason='seeded by the orga',
        initiator_id=users[0].id,
    ).unwrap()

    assert _phase_two(tournament)
    assert _committed_release(tournament).playoff_released_at is not None
    (entry,) = _log_entries(tournament, 'playoffs-released')
    assert entry.data['mode'] == 'automatic'


def test_unrelease_only_without_phase_two_results(make_tournament, users):
    tournament = make_tournament()
    _play_groups(tournament, users[0])
    assert _release(tournament, users[0]).is_ok()

    empty_reason = tournament_qualification_service.unrelease_playoffs(
        tournament.id, reason='  ', initiator_id=users[0].id
    )
    assert empty_reason.unwrap_err() == 'Please give a reason for the decision.'
    assert len(_phase_two(tournament)) == 4

    semifinal = next(m for m in _phase_two(tournament) if m.round == 0)
    contestants = tournament_repository.get_contestants_for_match(semifinal.id)
    _confirm(
        semifinal, [c.participant_id for c in contestants], (2, 1), users[0]
    )
    assert not _qualification(tournament).can_unrelease

    refused = tournament_qualification_service.unrelease_playoffs(
        tournament.id, reason='wrong seeding', initiator_id=users[0].id
    )

    assert (
        refused.unwrap_err()
        == tournament_qualification_service.ERR_CANNOT_UNRELEASE
    )
    assert len(_phase_two(tournament)) == 4
    assert _committed_release(tournament).playoff_released_at is not None
    assert not _log_entries(tournament, 'playoffs-unreleased')


def test_unrelease_deletes_phase_two_and_keeps_phase_one(
    make_tournament, users
):
    tournament = make_tournament()
    _play_groups(tournament, users[0])
    assert _release(tournament, users[0]).is_ok()
    deleted = []

    def receiver(sender, *, event):
        deleted.append(event.match_id)

    signals.match_deleted.connect(receiver, weak=False)
    try:
        result = tournament_qualification_service.unrelease_playoffs(
            tournament.id,
            reason=' seeding was wrong ',
            initiator_id=users[2].id,
        )
    finally:
        signals.match_deleted.disconnect(receiver)

    assert result.is_ok(), result.unwrap_err()
    assert len(deleted) == 4
    assert not _phase_two(tournament)
    assert sum(len(g) for g in _groups(tournament).values()) == 12
    assert all(
        m.confirmed_by is not None
        for matches in _groups(tournament).values()
        for m, _ in matches
    )
    row = _committed_release(tournament)
    assert row.playoff_released_at is None
    assert row.playoff_released_by is None
    assert row.playoff_auto_release_suspended is False
    (entry,) = _log_entries(tournament, 'playoffs-unreleased')
    assert entry.initiator_id == users[2].id
    assert entry.data['reason'] == 'seeding was wrong'
    assert _qualification(tournament).ready

    # The draft survives; a new manual release reuses it.
    assert tournament_seeding_repository.find_seeding(tournament.id, 'playoff')
    again = _release(tournament, users[0])
    assert again.is_ok(), again.unwrap_err()
    assert len(_phase_two(tournament)) == 4


def test_unrelease_suspends_auto(make_tournament, users):
    tournament = make_tournament(release_mode=PlayoffReleaseMode.AUTOMATIC)
    _play_groups(tournament, users[0])
    assert len(_phase_two(tournament)) == 4

    result = tournament_qualification_service.unrelease_playoffs(
        tournament.id, reason='re-seed by hand', initiator_id=users[0].id
    )

    assert result.is_ok(), result.unwrap_err()
    assert _committed_release(tournament).playoff_auto_release_suspended is True
    assert not _phase_two(tournament)

    attempt = tournament_qualification_service.try_auto_release(
        tournament.id, triggered_by=users[0].id
    )
    assert attempt.unwrap() is False
    assert not _phase_two(tournament)

    # Unconfirm and confirm again: the hooks must not release it either.
    match, ids = _groups(tournament)[0][0]
    tournament_match_service.unconfirm_match(match.id, users[0].id)
    tournament_match_service.admin_set_and_confirm_match(
        match.id, users[0].id, dict(zip(ids, (3, 0), strict=True))
    )
    assert not _phase_two(tournament)

    manual = _release(tournament, users[0])
    assert manual.is_ok(), manual.unwrap_err()
    assert len(_phase_two(tournament)) == 4


def test_release_sends_match_ready(make_tournament, users, ready_events):
    tournament = make_tournament()
    _play_groups(tournament, users[0])
    ready_events.clear()
    seen_committed = []

    def receiver(sender, *, event):
        seen_committed.append(
            _committed_release(tournament).playoff_released_at is not None
        )

    signals.match_ready.connect(receiver, weak=False)
    try:
        result = _release(tournament, users[0])
    finally:
        signals.match_ready.disconnect(receiver)

    assert result.is_ok(), result.unwrap_err()
    first_round = {m.id for m in _phase_two(tournament) if m.round == 0}
    assert {e.match_id for e in ready_events} == first_round
    assert len(ready_events) == 2
    assert seen_committed == [True, True]


def test_release_error_rolls_everything_back(
    make_tournament, users, ready_events, monkeypatch
):
    tournament = make_tournament()
    _play_groups(tournament, users[0])
    ready_events.clear()
    db.session.rollback()
    draft_before = tournament_seeding_repository.find_seeding(
        tournament.id, 'playoff'
    )
    assert draft_before is not None
    log_entries_before = _committed_count(_LOG_COUNT_SQL, tournament)
    real = tournament_seeding_service.stage_generation

    def failing_after_generation(*args, **kwargs):
        outcome = real(*args, **kwargs)
        assert outcome.is_ok(), outcome.unwrap_err()
        assert len(_phase_two(tournament)) == 4
        return Err('boom')

    monkeypatch.setattr(
        tournament_seeding_service, 'stage_generation', failing_after_generation
    )

    rollbacks = []
    real_rollback = tournament_repository.rollback_session
    monkeypatch.setattr(
        tournament_repository,
        'rollback_session',
        lambda: (rollbacks.append(True), real_rollback())[1],
    )

    result = tournament_qualification_service.release_playoffs(
        tournament.id, expected_version=1, initiator_id=users[0].id
    )

    assert result.unwrap_err() == 'boom'
    assert rollbacks
    assert (
        _committed_count(
            'SELECT count(*) FROM lan_tournament_matches'
            ' WHERE tournament_id = :id AND phase = 2',
            tournament,
        )
        == 0
    )
    assert _committed_release(tournament).playoff_released_at is None
    assert (
        _committed_count(
            'SELECT count(*) FROM lan_tournament_log_entries'
            ' WHERE tournament_id = :id AND event_type IN'
            " ('playoffs-released', 'bracket-generated')",
            tournament,
        )
        == 0
    )
    assert (
        _committed_count(_LOG_COUNT_SQL, tournament) == log_entries_before
    )
    db.session.rollback()
    draft_after = tournament_seeding_repository.find_seeding(
        tournament.id, 'playoff'
    )
    assert draft_after is not None
    assert draft_after.version == draft_before.version
    assert draft_after.seed_code == draft_before.seed_code
    assert draft_after.generated_seed_code is None
    assert ready_events == []


def test_full_row_update_keeps_the_release(make_tournament, users):
    tournament = make_tournament()
    _play_groups(tournament, users[0])
    assert _release(tournament, users[0]).is_ok()
    released = tournament_repository.get_tournament(tournament.id)
    assert released.playoff_released_at is not None

    tournament_repository.update_tournament(
        replace(released, name='Renamed after release')
    )
    tournament_repository.commit_session()

    row = _committed_release(tournament)
    assert row.playoff_released_at is not None
    assert row.playoff_released_by == users[0].id
    reloaded = tournament_repository.get_tournament(tournament.id)
    assert reloaded.name == 'Renamed after release'
    assert reloaded.playoff_released_at == row.playoff_released_at


def _flip_second_and_third(tournament, admin):
    """Reverse the result between the second and third of group 0."""
    before = _qualification(tournament)
    entries = next(r for r in before.rankings if r.scope == 'group:0').entries
    pair = {entries[1].contestant_id, entries[2].contestant_id}
    match, ids = next(
        (m, i) for m, i in _groups(tournament)[0] if {str(x) for x in i} == pair
    )
    flipped = (0, 3) if str(ids[0]) == entries[1].contestant_id else (3, 0)
    tournament_match_service.unconfirm_match(match.id, admin.id).unwrap()
    _confirm(match, ids, flipped, admin)
    assert {q.contestant_id for q in _qualification(tournament).qualifiers} != {
        q.contestant_id for q in before.qualifiers
    }


def test_untouched_playoff_draft_follows_changed_qualifiers(
    make_tournament, users
):
    tournament = make_tournament()
    _play_groups(tournament, users[0])
    first = tournament_seeding_service.ensure_playoff_draft(
        tournament.id
    ).unwrap()
    assert not first.stale

    _flip_second_and_third(tournament, users[0])

    board = tournament_seeding_service.ensure_playoff_draft(
        tournament.id
    ).unwrap()
    assert not board.stale
    assert board.version == first.version + 1
    assert set(board.state.roster) == {
        q.contestant_id for q in _qualification(tournament).qualifiers
    }
    released = tournament_qualification_service.release_playoffs(
        tournament.id, expected_version=board.version, initiator_id=users[0].id
    )
    assert released.is_ok(), released.unwrap_err()


def test_playoff_draft_turns_stale_when_qualifiers_change(
    make_tournament, users
):
    tournament = make_tournament()
    _play_groups(tournament, users[0])
    first = tournament_seeding_service.ensure_playoff_draft(
        tournament.id
    ).unwrap()
    assert not first.stale
    touched = tournament_seeding_service.apply_action(
        tournament.id,
        'playoff',
        tournament_seeding_service.Swap(0, 1),
        expected_version=first.version,
        initiator_id=users[0].id,
    ).unwrap()

    _flip_second_and_third(tournament, users[0])

    board = tournament_seeding_service.ensure_playoff_draft(
        tournament.id
    ).unwrap()

    assert board.stale
    assert board.version == touched.version
    refused = tournament_qualification_service.release_playoffs(
        tournament.id, expected_version=board.version, initiator_id=users[0].id
    )
    assert refused.unwrap_err() == tournament_seeding_service.ERR_STALE
    assert not _phase_two(tournament)


def test_double_elimination_playoffs_generate_as_phase_two(
    make_tournament, users
):
    tournament = make_tournament(
        playoff_mode=EliminationMode.DOUBLE_ELIMINATION
    )
    _play_groups(tournament, users[0])

    result = _release(tournament, users[0])

    assert result.is_ok(), result.unwrap_err()
    phase_two = _phase_two(tournament)
    assert len(phase_two) == result.unwrap() > 4
    assert len(phase_two) + sum(
        len(g) for g in _groups(tournament).values()
    ) == len(tournament_repository.get_matches_for_tournament(tournament.id))
    assert {m.bracket.value for m in phase_two} == {'WB', 'LB', 'GF'}


def test_phase_two_draw_is_refused(make_tournament, users):
    tournament = make_tournament()
    _play_groups(tournament, users[0])
    assert _release(tournament, users[0]).is_ok()
    semifinal = next(m for m in _phase_two(tournament) if m.round == 0)
    contestants = tournament_repository.get_contestants_for_match(semifinal.id)

    result = tournament_match_service.admin_set_and_confirm_match(
        semifinal.id,
        users[0].id,
        {c.participant_id: 1 for c in contestants},
    )

    assert result.unwrap_err() == (
        'Match is a draw; a winner is required in this tournament mode.'
    )


def test_last_phase_two_result_completes_the_tournament(make_tournament, users):
    tournament = make_tournament()
    _play_groups(tournament, users[0])
    assert _release(tournament, users[0]).is_ok()

    while True:
        ready = [
            (m, tournament_repository.get_contestants_for_match(m.id))
            for m in _phase_two(tournament)
            if m.confirmed_by is None
        ]
        ready = [(m, c) for m, c in ready if len(c) == 2]
        if not ready:
            break
        match, contestants = ready[0]
        _confirm(
            match, [c.participant_id for c in contestants], (2, 1), users[0]
        )

    final = tournament_repository.get_tournament(tournament.id)
    assert final.tournament_status is TournamentStatus.COMPLETED
    assert not _qualification(tournament).can_unrelease


def test_playoff_prefill_for_a_highscore_tournament_uses_tiers():
    qualifiers = [
        qualification_domain.Qualifier(
            contestant_id=f'c{i}', scope='leaderboard', rank=i + 1, row=None
        )
        for i in range(12)
    ]

    state = tournament_seeding_service._prefill_playoff_state(
        SeedingFormat.FREE_FOR_ALL,
        4,
        [q.contestant_id for q in qualifiers],
        {q.contestant_id: q.scope for q in qualifiers},
        draw_seed=7,
    )

    assert state.seed_list == tuple(f'c{i}' for i in range(12))
    assert state.tier_count == 3
    tier_of = dict(zip(state.roster, state.tiers, strict=True))
    assert [tier_of[c] for c in state.seed_list] == [0] * 4 + [1] * 4 + [2] * 4


def test_auto_release_hook_in_confirm_match(make_tournament, users):
    tournament = make_tournament(release_mode=PlayoffReleaseMode.AUTOMATIC)
    match, ids, scores = _play_groups(tournament, users[0], skip_last=True)
    assert not _phase_two(tournament)

    for contestant_id, score in zip(ids, scores, strict=True):
        result = tournament_match_service.set_score(
            match.id, contestant_id, score
        )
        assert result.is_ok(), result.unwrap_err()
    confirmed = tournament_match_service.confirm_match(match.id, users[0].id)
    assert confirmed.is_ok(), confirmed.unwrap_err()

    assert len(_phase_two(tournament)) == 4
    assert _committed_release(tournament).playoff_released_at is not None


def test_unconfirm_calls_the_auto_release_hook(
    make_tournament, users, monkeypatch
):
    tournament = make_tournament(release_mode=PlayoffReleaseMode.AUTOMATIC)
    _play_groups(tournament, users[0], skip_last=True)
    calls = []
    monkeypatch.setattr(
        tournament_qualification_service,
        'try_auto_release',
        lambda tournament_id, *, triggered_by: calls.append(
            (tournament_id, triggered_by)
        ),
    )
    match, _ = _groups(tournament)[0][0]

    result = tournament_match_service.unconfirm_match(match.id, users[0].id)

    assert result.is_ok(), result.unwrap_err()
    assert calls == [(tournament.id, users[0].id)]


def test_byes_do_not_block_unrelease(make_tournament, users):
    tournament = make_tournament(
        release_mode=PlayoffReleaseMode.AUTOMATIC, qualifiers_per_group=3
    )
    _play_groups(tournament, users[0])

    phase_two = _phase_two(tournament)
    assert _committed_release(tournament).playoff_released_at is not None
    contestants = tournament_repository.get_contestants_for_tournament(
        tournament.id
    )
    byes = [
        m
        for m in phase_two
        if m.confirmed_by is not None and len(contestants[m.id]) == 1
    ]
    assert len(byes) == 2
    assert _qualification(tournament).can_unrelease

    result = tournament_qualification_service.unrelease_playoffs(
        tournament.id, reason='re-seed by hand', initiator_id=users[0].id
    )

    assert result.is_ok(), result.unwrap_err()
    assert not _phase_two(tournament)


def test_regenerate_after_release_touches_phase_two_only(
    make_tournament, users
):
    tournament = make_tournament()
    _play_groups(tournament, users[0])
    assert _release(tournament, users[0]).is_ok()
    before = {m.id for m in _phase_two(tournament)}
    seeding = tournament_seeding_repository.find_seeding(
        tournament.id, 'playoff'
    )

    unchanged = tournament_seeding_service.generate_from_seeding(
        tournament.id,
        'playoff',
        expected_version=seeding.version,
        initiator_id=users[0].id,
    )
    assert unchanged.unwrap() == tournament_seeding_service.GENERATION_UNCHANGED
    edited = tournament_seeding_service.apply_action(
        tournament.id,
        'playoff',
        tournament_seeding_service.Swap(0, 1),
        expected_version=seeding.version,
        initiator_id=users[0].id,
    ).unwrap()

    result = tournament_seeding_service.generate_from_seeding(
        tournament.id,
        'playoff',
        expected_version=edited.version,
        initiator_id=users[0].id,
    )

    assert result.unwrap() == 4
    after = {m.id for m in _phase_two(tournament)}
    assert len(after) == 4 and not (after & before)
    assert sum(len(g) for g in _groups(tournament).values()) == 12
    assert all(
        m.confirmed_by is not None
        for matches in _groups(tournament).values()
        for m, _ in matches
    )
    assert len(_log_entries(tournament, 'bracket-regenerated')) == 1
    assert _committed_release(tournament).playoff_released_at is not None


def test_playoff_regenerate_with_a_stale_version_sends_no_ready(
    make_tournament, users, ready_events
):
    tournament = make_tournament()
    _play_groups(tournament, users[0])
    draft = tournament_seeding_service.ensure_playoff_draft(
        tournament.id
    ).unwrap()
    assert _release(tournament, users[0]).is_ok()
    current = tournament_seeding_repository.find_seeding(
        tournament.id, 'playoff'
    )
    assert current.version == draft.version + 1
    before = {m.id for m in _phase_two(tournament)}
    ready_events.clear()

    for _ in range(3):
        stale = tournament_seeding_service.generate_from_seeding(
            tournament.id,
            'playoff',
            expected_version=draft.version,
            initiator_id=users[0].id,
        )
        assert stale.unwrap_err() == tournament_seeding_service.ERR_CONFLICT
    assert ready_events == []

    unchanged = tournament_seeding_service.generate_from_seeding(
        tournament.id,
        'playoff',
        expected_version=current.version,
        initiator_id=users[0].id,
    )
    assert unchanged.unwrap() == tournament_seeding_service.GENERATION_UNCHANGED
    assert ready_events == []
    assert {m.id for m in _phase_two(tournament)} == before


def test_playoff_generation_needs_the_release_and_no_result(
    make_tournament, users
):
    tournament = make_tournament()
    _play_groups(tournament, users[0])
    board = tournament_seeding_service.ensure_playoff_draft(
        tournament.id
    ).unwrap()

    early = tournament_seeding_service.generate_from_seeding(
        tournament.id,
        'playoff',
        expected_version=board.version,
        initiator_id=users[0].id,
    )
    assert (
        early.unwrap_err()
        == tournament_seeding_service.ERR_PLAYOFF_NOT_RELEASED
    )
    assert not _phase_two(tournament)

    assert _release(tournament, users[0]).is_ok()
    semifinal = next(m for m in _phase_two(tournament) if m.round == 0)
    contestants = tournament_repository.get_contestants_for_match(semifinal.id)
    _confirm(
        semifinal, [c.participant_id for c in contestants], (2, 1), users[0]
    )

    late = tournament_seeding_service.generate_from_seeding(
        tournament.id,
        'playoff',
        expected_version=board.version,
        initiator_id=users[0].id,
    )
    assert late.unwrap_err() == tournament_seeding_service.ERR_PLAYOFF_LOCKED
    assert len(_phase_two(tournament)) == 4
    locked = tournament_seeding_service.ensure_playoff_draft(
        tournament.id
    ).unwrap()
    assert (
        locked.generation is tournament_seeding_service.GenerationStatus.LOCKED
    )


def test_withdraw_decision_calls_the_auto_release_hook(
    make_tournament, users, monkeypatch
):
    tournament = make_tournament(participants=4, qualifiers_per_group=1)
    for matches in _groups(tournament).values():
        for match, ids in matches:
            _confirm(match, ids, (1, 1), users[0])
    tie = _qualification(tournament).blockers[0]
    tournament_qualification_service.save_decision(
        tournament.id,
        tie.scope,
        list(tie.contestant_ids),
        reason='coin toss by the orga',
        initiator_id=users[0].id,
    ).unwrap()
    calls = []
    monkeypatch.setattr(
        tournament_qualification_service,
        'try_auto_release',
        lambda tournament_id, *, triggered_by: calls.append(
            (tournament_id, triggered_by)
        ),
    )

    result = tournament_qualification_service.withdraw_decision(
        tournament.id,
        tie.scope,
        contestant_ids=list(tie.contestant_ids),
        reason='changed our mind',
        initiator_id=users[0].id,
    )

    assert result.is_ok(), result.unwrap_err()
    assert calls == [(tournament.id, users[0].id)]


def test_bracket_reset_match_of_the_playoffs_is_phase_two(
    make_tournament, users
):
    tournament = make_tournament(
        playoff_mode=EliminationMode.DOUBLE_ELIMINATION,
        participants=4,
        qualifiers_per_group=2,
    )
    _play_groups(tournament, users[0])
    assert _release(tournament, users[0]).is_ok()

    def ready_matches():
        found = []
        for match in _phase_two(tournament):
            if match.confirmed_by is not None:
                continue
            contestants = tournament_repository.get_contestants_for_match(
                match.id
            )
            if len(contestants) == 2:
                found.append((match, contestants))
        return found

    def winner_of(match_id):
        contestants = tournament_repository.get_contestants_for_match(match_id)
        return max(contestants, key=lambda c: c.score).participant_id

    reset_seen = False
    for _ in range(20):
        ready = ready_matches()
        if not ready:
            break
        match, contestants = ready[0]
        ids = [c.participant_id for c in contestants]
        scores = (2, 1)
        if match.bracket.value == 'GF' and match.match_order == 0:
            wb_final = next(
                m
                for m in _phase_two(tournament)
                if m.bracket.value == 'WB' and m.next_match_id == match.id
            )
            wb_champion = winner_of(wb_final.id)
            scores = (1, 2) if ids[0] == wb_champion else (2, 1)
        _confirm(match, ids, scores, users[0])
        if match.bracket.value == 'GF' and match.match_order == 0:
            reset_seen = True
            break

    assert reset_seen
    second = [
        m
        for m in tournament_repository.get_matches_for_tournament(tournament.id)
        if m.bracket is not None
        and m.bracket.value == 'GF'
        and m.match_order == 1
    ]
    assert len(second) == 1
    assert second[0].phase == 2
    assert (
        tournament_repository.get_tournament(tournament.id).tournament_status
        is TournamentStatus.ONGOING
    )


def test_release_by_a_decision_confirms_the_byes_as_its_orga(
    make_tournament, users
):
    tournament = make_tournament(
        release_mode=PlayoffReleaseMode.AUTOMATIC, qualifiers_per_group=3
    )
    for group, matches in _groups(tournament).items():
        margin = group + 1
        members = sorted({i for _, ids in matches for i in ids}, key=str)
        rank = {member: n for n, member in enumerate(members)}
        for match, ids in matches:
            first, second = ids
            if {rank[first], rank[second]} == {2, 3}:
                scores = (margin, margin)
            elif rank[first] < rank[second]:
                scores = (margin, 0)
            else:
                scores = (0, margin)
            _confirm(match, ids, scores, users[0])
    blockers = _qualification(tournament).blockers
    assert {b.scope for b in blockers} == {'group:0', 'group:1'}
    assert not _phase_two(tournament)

    for blocker in blockers:
        tournament_qualification_service.save_decision(
            tournament.id,
            blocker.scope,
            list(blocker.contestant_ids),
            reason='coin toss by the orga',
            initiator_id=users[3].id,
        ).unwrap()

    contestants = tournament_repository.get_contestants_for_tournament(
        tournament.id
    )
    byes = [
        m
        for m in _phase_two(tournament)
        if m.confirmed_by is not None and len(contestants[m.id]) == 1
    ]
    assert len(byes) == 2
    assert {m.confirmed_by for m in byes} == {users[3].id}
    (entry,) = _log_entries(tournament, 'playoffs-released')
    assert entry.initiator_id is None
    assert entry.data['mode'] == 'automatic'
    assert entry.data['triggered_by'] == str(users[3].id)


def test_de_playoffs_with_three_qualifiers_release_as_single_elimination(
    make_tournament, users
):
    tournament = make_tournament(
        playoff_mode=EliminationMode.DOUBLE_ELIMINATION,
        participants=6,
        groups=3,
    )
    # The validator refuses a configured total below 4 for DE, so a
    # shortfall is staged by lowering the places after creation.
    current = tournament_repository.get_tournament(tournament.id)
    tournament_repository.update_tournament(
        replace(current, playoff_qualifiers_per_group=1)
    )
    tournament_repository.commit_session()
    for group, matches in _groups(tournament).items():
        for match, ids in matches:
            low, high = sorted(ids, key=str)
            scores = (group + 1, 0) if ids[0] == low else (0, group + 1)
            _confirm(match, ids, scores, users[0])
    board = tournament_seeding_service.ensure_playoff_draft(
        tournament.id
    ).unwrap()

    assert board.state.format is SeedingFormat.SINGLE_ELIMINATION
    assert len(board.state.roster) == 3
    assert board.problems == ()
    assert tournament_seeding_service.NOTICE_KNOCKOUT_FALLBACK in board.notices

    result = tournament_qualification_service.release_playoffs(
        tournament.id, expected_version=board.version, initiator_id=users[0].id
    )

    assert result.is_ok(), result.unwrap_err()
    assert _phase_two(tournament)


def test_mode_change_before_release_marks_the_playoff_draft_stale_and_reseed_keeps_the_layout(
    make_tournament, users
):
    tournament = make_tournament()
    _play_groups(tournament, users[0])
    draft = tournament_seeding_service.ensure_playoff_draft(
        tournament.id
    ).unwrap()
    swapped = tournament_seeding_service.apply_action(
        tournament.id,
        'playoff',
        tournament_seeding_service.Swap(0, 1),
        expected_version=draft.version,
        initiator_id=users[0].id,
    ).unwrap()
    assert swapped.state.format is SeedingFormat.SINGLE_ELIMINATION
    assert swapped.state.layout != draft.state.layout

    current = tournament_repository.get_tournament(tournament.id)
    tournament_repository.update_tournament(
        replace(
            current, playoff_elimination_mode=EliminationMode.DOUBLE_ELIMINATION
        )
    )
    tournament_repository.commit_session()

    stale = tournament_seeding_service.get_board(
        tournament.id, 'playoff'
    ).unwrap()
    assert stale.stale_structure
    assert stale.state.format is SeedingFormat.DOUBLE_ELIMINATION
    assert stale.state.layout == swapped.state.layout
    refused = tournament_qualification_service.release_playoffs(
        tournament.id, expected_version=stale.version, initiator_id=users[0].id
    )
    assert (
        refused.unwrap_err() == tournament_seeding_service.ERR_STRUCTURE_CHANGED
    )
    assert not _phase_two(tournament)

    reseeded = tournament_seeding_service.apply_action(
        tournament.id,
        'playoff',
        tournament_seeding_service.ReseedKeepTiers(),
        expected_version=stale.version,
        initiator_id=users[0].id,
    ).unwrap()
    assert not reseeded.stale_structure
    assert reseeded.state.format is SeedingFormat.DOUBLE_ELIMINATION
    assert reseeded.state.layout == swapped.state.layout

    released = tournament_qualification_service.release_playoffs(
        tournament.id,
        expected_version=reseeded.version,
        initiator_id=users[0].id,
    )
    assert released.is_ok(), released.unwrap_err()
    assert {m.bracket.value for m in _phase_two(tournament)} == {
        'WB',
        'LB',
        'GF',
    }


def test_playoff_reseed_and_regenerate_until_the_first_phase_two_result(
    make_tournament, users
):
    tournament = make_tournament()
    _play_groups(tournament, users[0])
    assert _release(tournament, users[0]).is_ok()

    def reseed():
        version = tournament_seeding_repository.find_seeding(
            tournament.id, 'playoff'
        ).version
        board = tournament_seeding_service.apply_action(
            tournament.id,
            'playoff',
            tournament_seeding_service.Swap(0, 1),
            expected_version=version,
            initiator_id=users[0].id,
        ).unwrap()
        return tournament_seeding_service.generate_from_seeding(
            tournament.id,
            'playoff',
            expected_version=board.version,
            initiator_id=users[0].id,
        )

    assert reseed().is_ok()

    semifinal = next(m for m in _phase_two(tournament) if m.round == 0)
    contestants = tournament_repository.get_contestants_for_match(semifinal.id)
    _confirm(
        semifinal, [c.participant_id for c in contestants], (2, 1), users[0]
    )

    version = tournament_seeding_repository.find_seeding(
        tournament.id, 'playoff'
    ).version
    locked = tournament_seeding_service.apply_action(
        tournament.id,
        'playoff',
        tournament_seeding_service.Swap(0, 1),
        expected_version=version,
        initiator_id=users[0].id,
    )
    assert locked.unwrap_err() == tournament_seeding_service.ERR_PLAYOFF_LOCKED


def _play_shrunk_groups(tournament, admin):
    """Play every group; the lower ID wins, with a margin of group + 1."""
    for group, matches in _groups(tournament).items():
        for match, ids in matches:
            low = min(ids, key=str)
            margin = group + 1
            scores = (margin, 0) if ids[0] == low else (0, margin)
            _confirm(match, ids, scores, admin)


def _make_shrunk(make_tournament, **overrides):
    """Return a tournament that configures 4 groups but forms 3."""
    tournament = make_tournament(
        participants=6, groups=4, qualifiers_per_group=1, **overrides
    )
    assert len(_groups(tournament)) == 3
    return tournament


@pytest.mark.parametrize(
    'mode', [PlayoffReleaseMode.MANUAL, PlayoffReleaseMode.AUTOMATIC]
)
def test_de_with_three_qualifiers_releases_as_single_elimination(
    make_tournament, users, mode
):
    tournament = _make_shrunk(
        make_tournament,
        release_mode=mode,
        playoff_mode=EliminationMode.DOUBLE_ELIMINATION,
    )

    _play_shrunk_groups(tournament, users[0])
    if mode is PlayoffReleaseMode.MANUAL:
        assert not _phase_two(tournament)
        released = _release(tournament, users[0])
        assert released.is_ok(), released.unwrap_err()

    phase_two = _phase_two(tournament)
    assert phase_two
    assert not {m.bracket for m in phase_two} & {
        Bracket.LOSERS,
        Bracket.GRAND_FINAL,
    }
    assert (
        tournament_repository.get_tournament(
            tournament.id
        ).playoff_elimination_mode
        is EliminationMode.SINGLE_ELIMINATION
    )
    (fallback,) = _log_entries(tournament, 'playoffs-de-fallback')
    triggered = mode is PlayoffReleaseMode.AUTOMATIC
    assert ('triggered_by' in fallback.data) is triggered
    assert {k: v for k, v in fallback.data.items() if k != 'triggered_by'} == {
        'qualified': 3,
        'from': 'DOUBLE_ELIMINATION',
        'to': 'SINGLE_ELIMINATION',
    }
    (shortfall,) = _log_entries(tournament, 'playoffs-shortfall')
    assert ('triggered_by' in shortfall.data) is triggered
    assert shortfall.data['configured'] == 4
    assert shortfall.data['qualified'] == 3
    (released_entry,) = _log_entries(tournament, 'playoffs-released')
    assert released_entry.data['qualifier_count'] == 3


def test_unrelease_after_the_fallback_keeps_single_elimination(
    make_tournament, users
):
    tournament = _make_shrunk(
        make_tournament, playoff_mode=EliminationMode.DOUBLE_ELIMINATION
    )
    _play_shrunk_groups(tournament, users[0])
    assert _release(tournament, users[0]).is_ok()

    undone = tournament_qualification_service.unrelease_playoffs(
        tournament.id, reason='check', initiator_id=users[0].id
    )

    assert undone.is_ok(), undone.unwrap_err()
    assert (
        tournament_repository.get_tournament(
            tournament.id
        ).playoff_elimination_mode
        is EliminationMode.SINGLE_ELIMINATION
    )


def test_a_full_field_release_logs_no_shortfall(make_tournament, users):
    tournament = make_tournament(
        playoff_mode=EliminationMode.DOUBLE_ELIMINATION
    )
    _play_groups(tournament, users[0])

    assert _release(tournament, users[0]).is_ok()

    assert not _log_entries(tournament, 'playoffs-shortfall')
    assert not _log_entries(tournament, 'playoffs-de-fallback')
    assert (
        tournament_repository.get_tournament(
            tournament.id
        ).playoff_elimination_mode
        is EliminationMode.DOUBLE_ELIMINATION
    )


def test_shortfall_release_gives_the_byes_to_the_top_seeds(
    make_tournament, users
):
    tournament = _make_shrunk(make_tournament)
    _play_shrunk_groups(tournament, users[0])

    released = _release(tournament, users[0])

    assert released.is_ok(), released.unwrap_err()
    contestants = tournament_repository.get_contestants_for_tournament(
        tournament.id
    )
    (bye,) = [
        m
        for m in _phase_two(tournament)
        if m.round == 0 and len(contestants.get(m.id, ())) == 1
    ]
    best_group = _groups(tournament)[2]
    best_winner = min((i for _, ids in best_group for i in ids), key=str)
    assert [c.participant_id for c in contestants[bye.id]] == [best_winner]
    (shortfall,) = _log_entries(tournament, 'playoffs-shortfall')
    assert shortfall.data == {'configured': 4, 'qualified': 3}


# -------------------------------------------------------------------- #
# the playoff draft follows the qualification


def _seed_order(tournament):
    return tuple(q.contestant_id for q in _qualification(tournament).seed_order)


def _flip_top_pair(tournament, admin, group=0, margin=3):
    """Reverse the result between the first and second place of a group."""
    entries = next(
        r
        for r in _qualification(tournament).rankings
        if r.scope == f'group:{group}'
    ).entries
    pair = {entries[0].contestant_id, entries[1].contestant_id}
    match, ids = next(
        (m, i)
        for m, i in _groups(tournament)[group]
        if {str(x) for x in i} == pair
    )
    winner = entries[1].contestant_id
    scores = tuple(margin if str(i) == winner else 0 for i in ids)
    tournament_match_service.unconfirm_match(match.id, admin.id).unwrap()
    _confirm(match, ids, scores, admin)


def _playoff_board(tournament):
    return tournament_seeding_service.get_board(
        tournament.id, 'playoff'
    ).unwrap()


def _apply_playoff(tournament, action, version, initiator):
    return tournament_seeding_service.apply_action(
        tournament.id,
        'playoff',
        action,
        expected_version=version,
        initiator_id=initiator.id,
    )


def _set_playoff_mode(tournament, mode):
    current = tournament_repository.get_tournament(tournament.id)
    tournament_repository.update_tournament(
        replace(current, playoff_elimination_mode=mode)
    )
    tournament_repository.commit_session()


def test_untouched_playoff_draft_follows_a_rank_swap(make_tournament, users):
    admin = users[0]
    tournament = make_tournament()
    _play_groups(tournament, admin)
    first = tournament_seeding_service.ensure_playoff_draft(
        tournament.id
    ).unwrap()
    order_before = _seed_order(tournament)
    assert first.state.seed_list == order_before

    _flip_top_pair(tournament, admin)

    order_after = _seed_order(tournament)
    assert order_after != order_before
    assert set(order_after) == set(order_before)
    board = _playoff_board(tournament)
    assert board.state.seed_list == order_after
    assert not board.stale
    assert board.version == first.version + 1
    (entry,) = _log_entries(tournament, 'seeding-reprefilled')
    assert entry.initiator_id is None
    assert entry.data['automatic'] is True
    assert entry.data['target'] == 'playoff'
    assert entry.data['version'] == board.version

    released = tournament_qualification_service.release_playoffs(
        tournament.id, expected_version=board.version, initiator_id=admin.id
    )
    assert released.is_ok(), released.unwrap_err()
    contestants = tournament_repository.get_contestants_for_tournament(
        tournament.id
    )
    pairs = {
        frozenset(str(c.participant_id) for c in contestants[m.id])
        for m in _phase_two(tournament)
        if len(contestants.get(m.id, ())) == 2
    }
    layout = board.state.layout
    assert pairs >= {
        frozenset((layout[i], layout[i + 1]))
        for i in range(0, len(layout), 2)
        if layout[i] is not None and layout[i + 1] is not None
    }


def test_touched_playoff_draft_turns_stale_on_a_rank_swap(
    make_tournament, users
):
    admin = users[0]
    tournament = make_tournament()
    _play_groups(tournament, admin)
    draft = tournament_seeding_service.ensure_playoff_draft(
        tournament.id
    ).unwrap()
    swapped = _apply_playoff(
        tournament, tournament_seeding_service.Swap(0, 1), draft.version, admin
    ).unwrap()

    _flip_top_pair(tournament, admin)

    board = _playoff_board(tournament)
    assert board.stale
    assert board.stale_ranks
    assert not board.stale_structure
    assert board.version == swapped.version
    assert not _log_entries(tournament, 'seeding-reprefilled')
    refused = tournament_qualification_service.release_playoffs(
        tournament.id, expected_version=board.version, initiator_id=admin.id
    )
    assert refused.unwrap_err() == tournament_seeding_service.ERR_RANKS_CHANGED
    assert not _phase_two(tournament)
    refused_action = _apply_playoff(
        tournament, tournament_seeding_service.Swap(0, 1), board.version, admin
    )
    assert (
        refused_action.unwrap_err()
        == tournament_seeding_service.ERR_RANKS_CHANGED
    )


def test_reseed_keep_tiers_rebaselines_a_ranks_stale_draft(
    make_tournament, users
):
    admin = users[0]
    tournament = make_tournament()
    _play_groups(tournament, admin)
    draft = tournament_seeding_service.ensure_playoff_draft(
        tournament.id
    ).unwrap()
    swapped = _apply_playoff(
        tournament, tournament_seeding_service.Swap(0, 1), draft.version, admin
    ).unwrap()
    _flip_top_pair(tournament, admin)
    stale = _playoff_board(tournament)
    assert stale.stale_ranks

    kept = _apply_playoff(
        tournament,
        tournament_seeding_service.ReseedKeepTiers(),
        stale.version,
        admin,
    ).unwrap()

    assert not kept.stale
    assert not kept.stale_ranks
    assert kept.state.layout == swapped.state.layout
    assert kept.state.seed_list == swapped.state.seed_list
    released = tournament_qualification_service.release_playoffs(
        tournament.id, expected_version=kept.version, initiator_id=admin.id
    )
    assert released.is_ok(), released.unwrap_err()


def test_a_touched_draft_keeps_its_swap_through_a_mode_change(
    make_tournament, users
):
    admin = users[0]
    tournament = make_tournament()
    _play_groups(tournament, admin)
    draft = tournament_seeding_service.ensure_playoff_draft(
        tournament.id
    ).unwrap()
    swapped = _apply_playoff(
        tournament, tournament_seeding_service.Swap(0, 1), draft.version, admin
    ).unwrap()

    _set_playoff_mode(tournament, EliminationMode.DOUBLE_ELIMINATION)

    stale = _playoff_board(tournament)
    assert stale.stale_structure
    assert not stale.stale_ranks
    assert stale.state.format is SeedingFormat.DOUBLE_ELIMINATION
    assert stale.state.layout == swapped.state.layout
    reseeded = _apply_playoff(
        tournament,
        tournament_seeding_service.ReseedKeepTiers(),
        stale.version,
        admin,
    ).unwrap()
    assert not reseeded.stale
    assert not reseeded.stale_ranks
    assert reseeded.state.format is SeedingFormat.DOUBLE_ELIMINATION
    assert reseeded.state.layout == swapped.state.layout
    released = tournament_qualification_service.release_playoffs(
        tournament.id, expected_version=reseeded.version, initiator_id=admin.id
    )
    assert released.is_ok(), released.unwrap_err()
    assert {m.bracket.value for m in _phase_two(tournament)} == {
        'WB',
        'LB',
        'GF',
    }


def test_an_untouched_draft_follows_a_mode_change_by_itself(
    make_tournament, users
):
    admin = users[0]
    tournament = make_tournament()
    _play_groups(tournament, admin)
    draft = tournament_seeding_service.ensure_playoff_draft(
        tournament.id
    ).unwrap()
    assert draft.state.format is SeedingFormat.SINGLE_ELIMINATION

    _set_playoff_mode(tournament, EliminationMode.DOUBLE_ELIMINATION)
    board = tournament_seeding_service.ensure_playoff_draft(
        tournament.id
    ).unwrap()

    assert board.state.format is SeedingFormat.DOUBLE_ELIMINATION
    assert not board.stale
    assert board.version == draft.version + 1
    assert board.state.seed_list == draft.state.seed_list
    (entry,) = _log_entries(tournament, 'seeding-reprefilled')
    assert entry.initiator_id is None
    assert entry.data['automatic'] is True
    released = tournament_qualification_service.release_playoffs(
        tournament.id, expected_version=board.version, initiator_id=admin.id
    )
    assert released.is_ok(), released.unwrap_err()


def test_the_release_rewrites_an_untouched_draft_and_wants_the_new_version(
    make_tournament, users
):
    admin = users[0]
    tournament = make_tournament()
    _play_groups(tournament, admin)
    draft = tournament_seeding_service.ensure_playoff_draft(
        tournament.id
    ).unwrap()
    _set_playoff_mode(tournament, EliminationMode.DOUBLE_ELIMINATION)

    stale_tab = tournament_qualification_service.release_playoffs(
        tournament.id, expected_version=draft.version, initiator_id=admin.id
    )

    assert stale_tab.unwrap_err() == tournament_seeding_service.ERR_CONFLICT
    assert not _phase_two(tournament)
    # The refused release rolled its rewrite back; the reload brings it.
    board = tournament_seeding_service.ensure_playoff_draft(
        tournament.id
    ).unwrap()
    assert board.state.format is SeedingFormat.DOUBLE_ELIMINATION
    assert board.version == draft.version + 1
    released = tournament_qualification_service.release_playoffs(
        tournament.id, expected_version=board.version, initiator_id=admin.id
    )
    assert released.is_ok(), released.unwrap_err()


def test_reprefill_action_rebuilds_from_the_qualification_and_is_audited(
    make_tournament, users
):
    admin = users[0]
    tournament = make_tournament()
    _play_groups(tournament, admin)
    draft = tournament_seeding_service.ensure_playoff_draft(
        tournament.id
    ).unwrap()
    swapped = _apply_playoff(
        tournament, tournament_seeding_service.Swap(0, 1), draft.version, admin
    ).unwrap()
    assert swapped.state.layout != draft.state.layout
    _flip_top_pair(tournament, admin)
    assert _playoff_board(tournament).stale_ranks

    rebuilt = _apply_playoff(
        tournament,
        tournament_seeding_service.Reprefill(),
        swapped.version,
        admin,
    ).unwrap()

    assert not rebuilt.stale
    assert not rebuilt.stale_ranks
    assert rebuilt.version == swapped.version + 1
    assert rebuilt.state.seed_list == _seed_order(tournament)
    assert rebuilt.state.layout != swapped.state.layout
    (entry,) = _log_entries(tournament, 'seeding-reprefilled')
    assert entry.initiator_id == admin.id
    assert 'automatic' not in entry.data
    assert entry.data['seed_code'] != entry.data['previous_seed_code']
    released = tournament_qualification_service.release_playoffs(
        tournament.id, expected_version=rebuilt.version, initiator_id=admin.id
    )
    assert released.is_ok(), released.unwrap_err()


def test_reprefill_is_refused_on_a_stale_version(make_tournament, users):
    admin = users[0]
    tournament = make_tournament()
    _play_groups(tournament, admin)
    draft = tournament_seeding_service.ensure_playoff_draft(
        tournament.id
    ).unwrap()

    refused = _apply_playoff(
        tournament,
        tournament_seeding_service.Reprefill(),
        draft.version + 1,
        admin,
    )

    assert refused.unwrap_err() == tournament_seeding_service.ERR_CONFLICT


def test_auto_suspended_draft_follows_the_qualification_after_unrelease(
    make_tournament, users
):
    admin = users[0]
    tournament = make_tournament(release_mode=PlayoffReleaseMode.AUTOMATIC)
    _play_groups(tournament, admin)
    assert _phase_two(tournament)
    unreleased = tournament_qualification_service.unrelease_playoffs(
        tournament.id, reason='fix a result', initiator_id=admin.id
    )
    assert unreleased.is_ok(), unreleased.unwrap_err()
    before = _playoff_board(tournament)
    order_before = _seed_order(tournament)
    assert before.state.seed_list == order_before

    _flip_top_pair(tournament, admin)

    assert not _phase_two(tournament)
    order_after = _seed_order(tournament)
    assert order_after != order_before
    board = _playoff_board(tournament)
    assert board.state.seed_list == order_after
    assert not board.stale
    assert board.version > before.version
    assert _log_entries(tournament, 'seeding-reprefilled')


def test_an_ffa_lobby_size_change_restales_with_a_new_prefill(party, users):
    admin = users[0]
    created = tournament_service.create_tournament(
        PARTY_ID,
        f'Playoff Release Highscore {next(_counter)}',
        contestant_type=ContestantType.SOLO,
        game_format=GameFormat.HIGHSCORE,
        elimination_mode=EliminationMode.NONE,
        score_ordering=ScoreOrdering.HIGHER_IS_BETTER,
        tournament_status=TournamentStatus.ONGOING,
        playoff_game_format=GameFormat.FREE_FOR_ALL,
        playoff_elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        playoff_qualifier_count=8,
        playoff_release_mode=PlayoffReleaseMode.MANUAL,
        point_table=[5, 3, 2, 1],
        group_size_min=3,
        group_size_max=4,
        advancement_count=2,
    )
    assert created.is_ok(), created.unwrap_err()
    tournament, _ = created.unwrap()
    try:
        ids = []
        for user in users[:8]:
            participant = TournamentParticipant(
                id=TournamentParticipantID(generate_uuid7()),
                user_id=user.id,
                tournament_id=tournament.id,
                substitute_player=False,
                team_id=None,
                created_at=datetime.now(UTC),
            )
            tournament_repository.create_participant(participant)
            ids.append(participant.id)
        db.session.commit()
        for score, participant_id in zip(range(10, 90, 10), ids, strict=True):
            tournament_score_service.submit_score(
                tournament.id, score, participant_id=participant_id
            ).unwrap()
        tournament_score_service.close_leaderboard(
            tournament.id, initiator_id=admin.id
        ).unwrap()
        draft = tournament_seeding_service.ensure_playoff_draft(
            tournament.id
        ).unwrap()
        assert draft.state.format is SeedingFormat.FREE_FOR_ALL
        assert draft.state.param == 4
        assert draft.state.tier_count == 2
        moved = _apply_playoff(
            tournament,
            tournament_seeding_service.MoveTier(draft.state.seed_list[0], 1),
            draft.version,
            admin,
        ).unwrap()
        assert moved.state.tiers != draft.state.tiers

        current = tournament_repository.get_tournament(tournament.id)
        tournament_repository.update_tournament(
            replace(current, group_size_max=5)
        )
        tournament_repository.commit_session()

        stale = _playoff_board(tournament)
        assert stale.stale_structure
        assert not stale.stale_ranks
        assert stale.state.param == 5
        assert stale.state.seed_list == draft.state.seed_list
        assert stale.state.tiers == draft.state.tiers
        assert stale.version == moved.version
        refused = tournament_qualification_service.release_playoffs(
            tournament.id, expected_version=stale.version, initiator_id=admin.id
        )
        assert (
            refused.unwrap_err()
            == tournament_seeding_service.ERR_STRUCTURE_CHANGED
        )
        assert not _phase_two(tournament)
    finally:
        db.session.rollback()
        tournament_service.delete_tournament(tournament.id)


@pytest.mark.parametrize(
    'terminal', [TournamentStatus.COMPLETED, TournamentStatus.CANCELLED]
)
def test_unrelease_is_refused_on_a_terminal_tournament(
    make_tournament, users, terminal
):
    tournament = make_tournament()
    _play_groups(tournament, users[0])
    assert _release(tournament, users[0]).is_ok()
    changed = tournament_service.change_status(
        tournament.id, terminal, users[0].id
    )
    assert changed.is_ok(), changed.unwrap_err()
    assert not _qualification(tournament).can_unrelease

    result = tournament_qualification_service.unrelease_playoffs(
        tournament.id, reason='oops', initiator_id=users[0].id
    )

    assert result == Err(tournament_qualification_service.ERR_UNRELEASE_STATUS)
    after = tournament_repository.get_tournament(tournament.id)
    assert after.tournament_status is terminal
    assert len(_phase_two(tournament)) == 4
    assert _committed_release(tournament).playoff_released_at is not None
    assert not _log_entries(tournament, 'playoffs-unreleased')


def test_unrelease_still_works_while_paused(make_tournament, users):
    tournament = make_tournament()
    _play_groups(tournament, users[0])
    assert _release(tournament, users[0]).is_ok()
    paused = tournament_service.change_status(
        tournament.id, TournamentStatus.PAUSED, users[0].id
    )
    assert paused.is_ok(), paused.unwrap_err()
    assert _qualification(tournament).can_unrelease

    result = tournament_qualification_service.unrelease_playoffs(
        tournament.id, reason='paused re-seed', initiator_id=users[0].id
    )

    assert result.is_ok(), result.unwrap_err()
    assert not _phase_two(tournament)
