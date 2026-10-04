"""
tests.integration.services.lan_tournament.test_generate_from_seeding
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from datetime import datetime, UTC
from itertools import count

import pytest
from sqlalchemy import text

from byceps.database import db
from byceps.services.lan_tournament import (
    signals,
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


PARTY_ID = PartyID('lan-party-2024-generate-from-seeding')

SE = (GameFormat.ONE_V_ONE, EliminationMode.SINGLE_ELIMINATION)
DE = (GameFormat.ONE_V_ONE, EliminationMode.DOUBLE_ELIMINATION)
RR = (GameFormat.ONE_V_ONE, EliminationMode.ROUND_ROBIN)
FFA = (GameFormat.FREE_FOR_ALL, EliminationMode.SINGLE_ELIMINATION)
FFA_KWARGS = {
    'max_players': 8,
    'group_size_min': 2,
    'group_size_max': 4,
    'advancement_count': 2,
    'point_table': [10, 6, 3, 1],
}

_counter = count(1)


@pytest.fixture(scope='module')
def party(make_party, make_brand):
    brand = make_brand(
        'generatefromseedingbrand', 'Generate From Seeding Brand'
    )
    return make_party(brand, PARTY_ID, 'LAN Party 2024 Generate From Seeding')


@pytest.fixture(scope='module')
def users(make_user):
    return [make_user(f'GenerateFromSeedingUser{i}') for i in range(8)]


@pytest.fixture
def make_tournament(party, users):
    created = []

    def _make(mode=SE, participants=8, **kwargs):
        result = tournament_service.create_tournament(
            PARTY_ID,
            f'Generate From Seeding Tournament {next(_counter)}',
            contestant_type=ContestantType.SOLO,
            game_format=mode[0],
            elimination_mode=mode[1],
            tournament_status=TournamentStatus.REGISTRATION_CLOSED,
            **kwargs,
        )
        assert result.is_ok(), result.unwrap_err()
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


@pytest.fixture
def captured_signals():
    events = {'created': [], 'deleted': [], 'ready': []}
    receivers = {
        'created': lambda _sender, event: events['created'].append(event),
        'deleted': lambda _sender, event: events['deleted'].append(event),
        'ready': lambda _sender, event: events['ready'].append(event),
    }
    sources = {
        'created': signals.match_created,
        'deleted': signals.match_deleted,
        'ready': signals.match_ready,
    }
    for name, signal in sources.items():
        signal.connect(receivers[name])
    yield events
    for name, signal in sources.items():
        signal.disconnect(receivers[name])


def _board(tournament):
    db.session.rollback()
    return svc.get_board(tournament.id).unwrap()


def _action(tournament, board, action, admin):
    result = svc.apply_action(
        tournament.id,
        'initial',
        action,
        expected_version=board.version,
        initiator_id=admin.id,
    )
    assert result.is_ok(), result.unwrap_err()
    return result.unwrap()


def _generate(tournament, board, admin):
    return svc.generate_from_seeding(
        tournament.id,
        expected_version=board.version,
        initiator_id=admin.id,
    )


def _confirm_round_zero(tournament, admin):
    matches = tournament_repository.get_matches_for_tournament(tournament.id)
    contestants = tournament_repository.get_contestants_for_matches(
        [m.id for m in matches]
    )
    match = next(m for m in matches if m.round == 0 and len(contestants[m.id]) == 2)
    scores = {c.participant_id: i for i, c in enumerate(contestants[match.id])}
    assert tournament_match_service.admin_set_and_confirm_match(
        match.id, admin.id, scores
    ).is_ok()
    return match.id, scores


def test_initial_regeneration_refused_while_a_result_is_confirmed(
    make_tournament, users, captured_signals, monkeypatch
):
    tournament, _ = make_tournament()
    admin = users[0]
    assert _generate(tournament, _board(tournament), admin).is_ok()
    match_id, scores = _confirm_round_zero(tournament, admin)
    before_code = _generated_code(tournament)
    board = _action(tournament, _board(tournament), svc.Swap(0, 7), admin)
    before_version = board.version
    for events in captured_signals.values():
        events.clear()

    order = []
    real_lock = tournament_repository.lock_tournament_for_update
    real_has_result = svc._has_confirmed_result

    def lock(tournament_id):
        result = real_lock(tournament_id)
        order.append('locked')
        return result

    def has_result(tournament_id):
        order.append('checked')
        return real_has_result(tournament_id)

    monkeypatch.setattr(tournament_repository, 'lock_tournament_for_update', lock)
    monkeypatch.setattr(svc, '_has_confirmed_result', has_result)

    result = _generate(tournament, board, admin)

    assert result.is_err()
    assert order == ['locked', 'checked']
    assert result.unwrap_err() == svc.ERR_RESULTS_EXIST
    board = _board(tournament)
    assert board.regenerate_refusal == svc.ERR_RESULTS_EXIST
    assert board.version == before_version
    assert _generated_code(tournament) == before_code
    match = tournament_repository.get_match(match_id)
    assert match.confirmed_by == admin.id
    contestants = tournament_repository.get_contestants_for_matches([match_id])
    assert {c.participant_id: c.score for c in contestants[match_id]} == scores
    assert _entries(tournament, 'bracket-regenerated') == []
    assert captured_signals == {'created': [], 'deleted': [], 'ready': []}


def test_locked_initial_board_has_no_regenerate_refusal(make_tournament, users):
    tournament, _ = make_tournament()
    admin = users[0]
    assert _generate(tournament, _board(tournament), admin).is_ok()
    _confirm_round_zero(tournament, admin)
    assert _board(tournament).regenerate_refusal == svc.ERR_RESULTS_EXIST
    assert tournament_repository.set_tournament_status_flush(
        tournament.id, TournamentStatus.ONGOING
    ).is_ok()
    db.session.commit()
    board = _board(tournament)
    assert board.generation is svc.GenerationStatus.LOCKED
    assert board.regenerate_refusal is None


def test_initial_regeneration_ignores_confirmed_byes(make_tournament, users):
    tournament, _ = make_tournament(participants=6)
    admin = users[0]
    assert _generate(tournament, _board(tournament), admin).is_ok()
    matches = tournament_repository.get_matches_for_tournament(tournament.id)
    contestants = tournament_repository.get_contestants_for_matches([m.id for m in matches])
    confirmed = [m for m in matches if m.confirmed_by is not None]
    assert len(confirmed) == 2
    assert all(len(contestants[m.id]) == 1 for m in confirmed)
    board = _action(tournament, _board(tournament), svc.Swap(0, 7), admin)
    assert board.regenerate_refusal is None
    assert _generate(tournament, board, admin).is_ok()


def test_confirmed_result_preserves_unchanged_no_op(make_tournament, users):
    tournament, _ = make_tournament()
    admin = users[0]
    assert _generate(tournament, _board(tournament), admin).is_ok()
    match_id, _ = _confirm_round_zero(tournament, admin)
    board = _board(tournament)
    assert _generate(tournament, board, admin).unwrap() == svc.GENERATION_UNCHANGED
    assert _board(tournament).version == board.version
    assert tournament_repository.get_match(match_id).confirmed_by == admin.id


def test_initial_regeneration_ignores_one_contestant_default_win(
    make_tournament, users
):
    tournament, _ = make_tournament()
    admin = users[0]
    assert _generate(tournament, _board(tournament), admin).is_ok()
    matches = tournament_repository.get_matches_for_tournament(tournament.id)
    match = next(m for m in matches if m.round == 0)
    contestants = tournament_repository.get_contestants_for_matches([match.id])
    # Represent a confirmed default win without changing the registered roster.
    tournament_repository.delete_match_contestant(contestants[match.id][1].id)
    tournament_repository.confirm_match(match.id, admin.id)
    db.session.commit()
    assert len(tournament_repository.get_contestants_for_matches([match.id])[match.id]) == 1
    board = _action(tournament, _board(tournament), svc.Swap(0, 7), admin)
    assert board.regenerate_refusal is None
    assert _generate(tournament, board, admin).is_ok()


def _placement(tournament):
    """Return the bracket as comparable data, blind to slot order in a match."""
    db.session.rollback()
    matches = tournament_match_service.get_matches_for_tournament(tournament.id)
    contestants = tournament_match_service.get_contestants_for_matches(
        [m.id for m in matches]
    )
    return sorted(
        (
            str(m.bracket),
            m.round,
            m.match_order,
            -1 if m.group_order is None else m.group_order,
            tuple(
                sorted(str(c.participant_id) for c in contestants.get(m.id, []))
            ),
        )
        for m in matches
    )


def _generated_code(tournament):
    db.session.rollback()
    return tournament_seeding_repository.find_seeding(
        tournament.id, 'initial'
    ).generated_seed_code


def _entries(tournament, event_type):
    db.session.rollback()
    return [
        e
        for e in tournament_log_service.get_entries_for_tournament(
            tournament.id
        )
        if e.event_type == event_type
    ]


@pytest.mark.parametrize(
    ('mode', 'kwargs'),
    [(SE, {}), (DE, {}), (RR, {}), (FFA, FFA_KWARGS)],
    ids=['se', 'de', 'rr', 'ffa'],
)
def test_generate_from_seeding_replay_identical(
    make_tournament, users, mode, kwargs
):
    admin = users[0]
    tournament, _ = make_tournament(mode, **kwargs)
    board = _board(tournament)

    assert _generate(tournament, board, admin).is_ok()
    first = _placement(tournament)
    first_code = _generated_code(tournament)
    assert first

    board = _action(tournament, _board(tournament), svc.Swap(0, 4), admin)
    assert _generate(tournament, board, admin).is_ok()
    assert _generated_code(tournament) != first_code
    if mode == RR:
        # One group, members stored in contestant-ID order: a swap inside it
        # changes the code but not the matches.
        assert _placement(tournament) == first
    else:
        assert _placement(tournament) != first

    board = _action(
        tournament, _board(tournament), svc.Replay(first_code), admin
    )
    assert _generate(tournament, board, admin).is_ok()

    assert _generated_code(tournament) == first_code
    assert _placement(tournament) == first


def test_generate_creates_the_layout_bracket(make_tournament, users):
    tournament, _ = make_tournament(SE, participants=4)
    board = _board(tournament)
    a, b, c, d = board.state.roster
    board = _action(tournament, board, svc.Swap(0, 3), users[0])
    layout = board.state.layout

    assert _generate(tournament, board, users[0]).is_ok()

    db.session.rollback()
    matches = tournament_match_service.get_matches_for_tournament(tournament.id)
    round_zero = sorted(
        (m for m in matches if m.round == 0), key=lambda m: m.match_order
    )
    contestants = tournament_match_service.get_contestants_for_matches(
        [m.id for m in round_zero]
    )
    placed = [
        [str(c.participant_id) for c in contestants[m.id]] for m in round_zero
    ]
    assert {frozenset(p) for p in placed} == {
        frozenset(layout[0:2]),
        frozenset(layout[2:4]),
    }
    assert {a, b, c, d} == {cid for pair in placed for cid in pair}


@pytest.mark.parametrize(
    'status',
    [
        TournamentStatus.DRAFT,
        TournamentStatus.REGISTRATION_OPEN,
        TournamentStatus.ONGOING,
        TournamentStatus.PAUSED,
        TournamentStatus.COMPLETED,
        TournamentStatus.CANCELLED,
    ],
)
def test_generate_only_in_registration_closed(make_tournament, users, status):
    tournament, _ = make_tournament()
    board = _board(tournament)
    assert tournament_repository.set_tournament_status_flush(
        tournament.id, status
    ).is_ok()
    db.session.commit()

    result = _generate(tournament, board, users[0])

    assert result.is_err()
    expected = (
        svc.ERR_LOCKED
        if status
        in (
            TournamentStatus.ONGOING,
            TournamentStatus.PAUSED,
            TournamentStatus.COMPLETED,
            TournamentStatus.CANCELLED,
        )
        else svc.ERR_NOT_OPEN_YET
    )
    assert result.unwrap_err() == expected
    assert _placement(tournament) == []
    assert _generated_code(tournament) is None
    assert _entries(tournament, 'bracket-generated') == []


def test_generate_in_registration_closed_succeeds(make_tournament, users):
    tournament, _ = make_tournament()

    result = _generate(tournament, _board(tournament), users[0])

    assert result.is_ok()
    assert result.unwrap() == 8  # 7 bracket matches + the third-place match


def test_generate_with_outdated_version_errs(make_tournament, users):
    tournament, _ = make_tournament()
    board = _board(tournament)
    _action(tournament, board, svc.Redraw(), users[0])

    result = _generate(tournament, board, users[0])

    assert result.unwrap_err() == svc.ERR_CONFLICT
    assert _placement(tournament) == []


def test_regenerate_logs_previous_code(
    make_tournament, users, captured_signals
):
    admin = users[0]
    tournament, _ = make_tournament()

    assert _generate(tournament, _board(tournament), admin).is_ok()
    first_code = _generated_code(tournament)
    assert len(captured_signals['created']) == 8
    assert captured_signals['deleted'] == []
    first_ids = {
        m.id
        for m in tournament_match_service.get_matches_for_tournament(
            tournament.id
        )
    }

    board = _action(tournament, _board(tournament), svc.Redraw(), admin)
    assert _generate(tournament, board, admin).is_ok()
    second_code = _generated_code(tournament)

    assert second_code != first_code
    (first_entry,) = _entries(tournament, 'bracket-generated')
    (second_entry,) = _entries(tournament, 'bracket-regenerated')
    assert first_entry.data['seed_code'] == first_code
    assert first_entry.data['previous_seed_code'] is None
    assert second_entry.data['seed_code'] == second_code
    assert second_entry.data['previous_seed_code'] == first_code
    assert {e.match_id for e in captured_signals['deleted']} == first_ids
    assert len(captured_signals['created']) == 16
    assert captured_signals['ready']


def test_generation_blocked_when_stale(make_tournament, users):
    tournament, ids = make_tournament()
    board = _board(tournament)
    assert tournament_participant_service.admin_remove_participant(
        tournament.id, ids[3]
    ).is_ok()
    db.session.rollback()

    result = _generate(tournament, board, users[0])

    assert result.unwrap_err() == svc.ERR_STALE
    assert _placement(tournament) == []
    assert _generated_code(tournament) is None


def test_generation_blocked_when_invalid(make_tournament, users):
    admin = users[0]
    tournament, _ = make_tournament(SE, participants=5)
    board = _board(tournament)
    layout = board.state.layout
    pairs = [layout[i : i + 2] for i in range(0, len(layout), 2)]
    with_bye = [i for i, pair in enumerate(pairs) if None in pair]
    first, second = with_bye[:2]
    contestant_slot = 2 * first + pairs[first].index(
        next(c for c in pairs[first] if c is not None)
    )
    bye_slot = 2 * second + pairs[second].index(None)
    board = _action(
        tournament, board, svc.Swap(contestant_slot, bye_slot), admin
    )
    assert board.problems

    result = _generate(tournament, board, admin)

    assert result.unwrap_err() == svc.ERR_PROBLEMS
    assert _placement(tournament) == []
    assert _generated_code(tournament) is None


@pytest.mark.parametrize('regenerate', [False, True], ids=['first', 'again'])
def test_generate_err_rolls_back_and_sends_no_signal(
    make_tournament, users, captured_signals, monkeypatch, regenerate
):
    admin = users[0]
    tournament, _ = make_tournament()
    previous = None
    if regenerate:
        assert _generate(tournament, _board(tournament), admin).is_ok()
        previous = _placement(tournament)
    board = _action(tournament, _board(tournament), svc.Redraw(), admin)
    before_code = _generated_code(tournament)
    captured_signals['created'].clear()
    captured_signals['deleted'].clear()
    captured_signals['ready'].clear()
    cleared_before = len(_entries(tournament, 'bracket-cleared'))

    real_impl = tournament_match_service._generate_single_elimination_impl

    def failing_impl(*args, **kwargs):
        assert real_impl(*args, **kwargs).is_ok()
        return Err('generation failed late')

    monkeypatch.setattr(
        tournament_match_service,
        '_generate_single_elimination_impl',
        failing_impl,
    )
    rollbacks = []
    real_rollback = tournament_repository.rollback_session
    monkeypatch.setattr(
        tournament_repository,
        'rollback_session',
        lambda: (rollbacks.append(1), real_rollback())[1],
    )

    result = _generate(tournament, board, admin)

    assert result.unwrap_err() == 'generation failed late'
    assert rollbacks
    # Read through the same session, without a rollback of the test's own:
    # only the service's rollback can have discarded the flushed rows.
    matches_in_session = len(
        tournament_match_service.get_matches_for_tournament(tournament.id)
    )
    assert matches_in_session == (len(previous) if previous else 0)
    with db.engine.connect() as connection:
        durable = connection.execute(
            text(
                'SELECT count(*) FROM lan_tournament_matches'
                ' WHERE tournament_id = :tid'
            ),
            {'tid': tournament.id},
        ).scalar_one()
        assert durable == matches_in_session
    assert _generated_code(tournament) == before_code
    assert len(_entries(tournament, 'bracket-cleared')) == cleared_before
    assert _entries(tournament, 'bracket-regenerated') == []
    assert len(_entries(tournament, 'bracket-generated')) == (
        1 if regenerate else 0
    )
    if regenerate:
        assert _placement(tournament) == previous
    assert captured_signals == {'created': [], 'deleted': [], 'ready': []}


def test_a_second_generate_with_the_same_version_is_refused(
    make_tournament, users
):
    admin = users[0]
    tournament, _ = make_tournament()
    board = _board(tournament)

    assert _generate(tournament, board, admin).is_ok()
    placed = _placement(tournament)
    ids = {
        m.id
        for m in tournament_match_service.get_matches_for_tournament(
            tournament.id
        )
    }

    result = _generate(tournament, board, admin)

    assert result.unwrap_err() == svc.ERR_CONFLICT
    db.session.rollback()
    assert {
        m.id
        for m in tournament_match_service.get_matches_for_tournament(
            tournament.id
        )
    } == ids
    assert _placement(tournament) == placed
    assert _entries(tournament, 'bracket-regenerated') == []
    assert _entries(tournament, 'bracket-cleared') == []


def test_generate_with_an_unchanged_code_is_a_no_op(
    make_tournament, users, captured_signals
):
    admin = users[0]
    tournament, _ = make_tournament()
    assert _generate(tournament, _board(tournament), admin).is_ok()
    board = _board(tournament)
    assert board.version == 2
    ids = {
        m.id
        for m in tournament_match_service.get_matches_for_tournament(
            tournament.id
        )
    }
    log_count = len(
        tournament_log_service.get_entries_for_tournament(tournament.id)
    )
    for events in captured_signals.values():
        events.clear()

    result = _generate(tournament, board, admin)

    assert result.unwrap() == svc.GENERATION_UNCHANGED
    after = _board(tournament)
    assert after.version == board.version
    assert {
        m.id
        for m in tournament_match_service.get_matches_for_tournament(
            tournament.id
        )
    } == ids
    assert (
        len(tournament_log_service.get_entries_for_tournament(tournament.id))
        == log_count
    )
    assert captured_signals == {'created': [], 'deleted': [], 'ready': []}
