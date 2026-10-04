"""Real PostgreSQL transactions and fully decorated security-fix routes.

Synchronization is at acquisition/operation boundaries, never after a COUNT
which a serialized contender cannot reach. Every worker owns its app context.
"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, UTC
import os
import threading
import time
from uuid import uuid4

from flask_babel import get_timezone
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DataError

from byceps.config.converter import assemble_database_uri
from byceps.database import db
from byceps.services.lan_tournament import (
    signals,
    tournament_log_service,
    tournament_match_service,
    tournament_orga_service,
    tournament_repository,
    tournament_request_repository,
    tournament_request_service,
    tournament_seeding_service,
    tournament_service,
)
from byceps.services.lan_tournament.blueprints.site import views as site_views
from byceps.services.lan_tournament.dbmodels.tournament import DbTournament
from byceps.services.lan_tournament.models import (
    ContestantType,
    EliminationMode,
    GameFormat,
    TournamentStatus,
)
from byceps.services.lan_tournament.models.bracket import Bracket
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipant,
    TournamentParticipantID,
)
from byceps.services.party.models import PartyID
from byceps.util.uuid import generate_uuid7

from tests.helpers import http_client, log_in_user
from tests.integration import conftest as inherited


DATABASE = 'byceps_test_prd_fixes'
DENIED = 'A completed tournament can only be reopened by an administrator.'
QUOTA = 'Too many open tournament requests.'
_local = threading.local()


def _verify_database_at_collection():
    """Refuse BEFORE any inherited fixture can drop/create tables.

    Do not override `database`: a second fixture would recreate the schema
    halfway through the full suite and invalidate its session-scoped data.
    """
    database_config = inherited.database_config.__wrapped__()
    assert os.environ.get('POSTGRES_DB') == DATABASE
    assert database_config.database == DATABASE
    engine = create_engine(
        assemble_database_uri(database_config),
        connect_args={'connect_timeout': 5},
    )
    try:
        with engine.connect() as connection:
            assert (
                connection.scalar(text('SELECT current_database()')) == DATABASE
            )
            assert (
                connection.scalar(text('SHOW transaction_isolation'))
                == 'read committed'
            )
    finally:
        engine.dispose()


_verify_database_at_collection()


def _limits():
    # Transaction-local settings disappear after rollback; acquisition hooks
    # call this again for every retry, not just at worker startup.
    db.session.execute(text("SET LOCAL lock_timeout = '8s'"))
    db.session.execute(text("SET LOCAL statement_timeout = '12s'"))
    assert db.session.scalar(text('SELECT current_database()')) == DATABASE
    assert (
        db.session.scalar(text('SHOW transaction_isolation'))
        == 'read committed'
    )
    return db.session.scalar(text('SELECT pg_backend_pid()'))


def _is_worker(label):
    return getattr(_local, 'label', None) == label


@contextmanager
def _workers(app):
    """Bounded futures propagate exceptions; release gates even on assertion failure."""
    releases = []
    futures = []
    executor = ThreadPoolExecutor(max_workers=3)

    def spawn(label, call, *gates):
        releases.extend(gates)
        ready = threading.Event()
        state = {}

        def run():
            _local.label = label
            _local.state = state
            try:
                with app.app_context():
                    try:
                        state['session'] = db.session()
                        state['pid'] = _limits()
                        ready.set()
                        return call()
                    finally:
                        db.session.rollback()
                        db.session.remove()
            finally:
                ready.set()
                for gate in gates:
                    gate.set()
                _local.label = None
                _local.state = None

        future = executor.submit(run)
        futures.append(future)
        assert ready.wait(15), 'worker initialization never finished'
        if 'pid' not in state:
            future.result(timeout=15)
        return future, state

    try:
        yield spawn
    finally:
        for gate in releases:
            gate.set()
        try:
            for future in futures:
                future.result(timeout=20)
        finally:
            executor.shutdown(wait=True, cancel_futures=True)


def _blocked_or_finished(future, waiter, holder):
    """Observe this exact wait edge, not an unrelated backend's lock wait.

    A lockless behavioral mutant can finish immediately: return False so the
    caller can release the holder and assert the wrong business outcome, rather
    than count a test timeout as mutation sensitivity.
    """
    assert waiter['pid'] != holder['pid']
    assert waiter['session'] is not holder['session']
    with db.engine.connect().execution_options(
        isolation_level='AUTOCOMMIT'
    ) as connection:
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            blockers = connection.scalar(
                text('SELECT pg_blocking_pids(:pid)'), {'pid': waiter['pid']}
            )
            if holder['pid'] in blockers:
                print(
                    f'PG wait edge: waiter={waiter["pid"]} holder={holder["pid"]}'
                )
                return True
            if future.done():
                future.result(timeout=1)
                return False
    pytest.fail(
        f'no correlated PG wait edge {waiter["pid"]} -> {holder["pid"]}'
    )


def _post(app, user, path, data=None):
    with http_client(app, user_id=user.id) as client:
        response = client.post(
            path, data=data or {}, headers={'Accept-Language': 'en'}
        )
        with client.session_transaction() as session:
            flashes = session.get('_flashes', [])
    return (
        response.status_code,
        response.location,
        [
            str(
                message.get('text', message)
                if isinstance(message, dict)
                else message
            )
            for _, message in flashes
        ],
    )


@pytest.fixture(scope='module')
def actors(make_user, make_admin):
    suffix = uuid4().hex
    orga = make_user(f'SecOrga{suffix[:12]}')
    peer = make_user(f'SecPeer{suffix[:12]}')
    admin = make_admin(
        {'admin.access', 'lan_tournament.administrate', 'lan_tournament.create'}
    )
    for user in (orga, peer, admin):
        log_in_user(user.id)
    return orga, peer, admin


@pytest.fixture
def quota_scope(make_brand, make_party, make_user):
    suffix = uuid4().hex
    brand = make_brand(f'sec-{suffix}', f'Security {suffix}')
    party = make_party(
        brand, PartyID(f'sec-{suffix}'), f'Security party {suffix}'
    )
    return party, make_user(f'Quota{suffix[:12]}')


@pytest.fixture
def make_tournament(party, actors):
    orga, peer, _ = actors

    def create(mode=None):
        ffa = mode is not None
        tournament, _ = tournament_service.create_tournament(
            party.id,
            f'Security race {uuid4()}',
            contestant_type=ContestantType.SOLO,
            game_format=GameFormat.FREE_FOR_ALL
            if ffa
            else GameFormat.ONE_V_ONE,
            elimination_mode=mode if ffa else EliminationMode.ROUND_ROBIN,
            tournament_status=TournamentStatus.REGISTRATION_CLOSED,
            **(
                dict(
                    point_table=[5, 3],
                    group_size_min=2,
                    group_size_max=2,
                    advancement_count=1,
                )
                if ffa
                else {}
            ),
        ).unwrap()
        for user in (orga, peer):
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
        tournament_orga_service.assign_orga(
            tournament.id, orga.id, orga.id
        ).unwrap()
        board = tournament_seeding_service.get_board(
            tournament.id, initiator_id=orga.id
        ).unwrap()
        tournament_seeding_service.generate_from_seeding(
            tournament.id,
            expected_version=board.version,
            initiator_id=orga.id,
        ).unwrap()
        tournament_service.change_status(
            tournament.id, TournamentStatus.ONGOING, orga.id
        ).unwrap()
        if mode is EliminationMode.DOUBLE_ELIMINATION:
            (wb,) = tournament_repository.get_matches_for_tournament(
                tournament.id
            )
            ids = sorted(
                str(c.participant_id)
                for c in tournament_repository.get_contestants_for_match(wb.id)
            )
            tournament_match_service.set_and_confirm_ffa_match(
                wb.id,
                dict(zip(ids, (1, 2), strict=True)),
                orga.id,
            ).unwrap()
            tournament_match_service.advance_ffa_round(
                tournament.id, pool=Bracket.WINNERS
            ).unwrap()
            db.session.rollback()  # eligibility answer keeps its tournament lock
            tournament_match_service.generate_ffa_grand_final(
                tournament.id, initiator_id=orga.id
            ).unwrap()
        db.session.rollback()
        return tournament

    yield create
    db.session.rollback()


def _final(tournament):
    matches = tournament_repository.get_matches_for_tournament(tournament.id)
    unconfirmed = [m for m in matches if m.confirmed_by is None]
    assert len(unconfirmed) == 1
    match = unconfirmed[0]
    ids = sorted(
        str(c.participant_id)
        for c in tournament_repository.get_contestants_for_match(match.id)
    )
    assert len(ids) == 2
    db.session.rollback()
    return match, dict(zip(ids, (1, 2), strict=True))


def _state(app, tournament, match=None):
    with app.app_context():
        try:
            found = tournament_repository.get_tournament(tournament.id)
            entries = tournament_log_service.get_entries_for_tournament(
                tournament.id
            )
            if match is None:
                return found, entries
            contestants = tournament_repository.get_contestants_for_match(
                match.id
            )
            return (
                found,
                tournament_repository.get_match(match.id),
                {
                    str(c.participant_id): (c.placement, c.points, c.score)
                    for c in contestants
                },
                entries,
            )
        finally:
            db.session.rollback()
            db.session.remove()


@contextmanager
def _events(*names, observe=None):
    received = {name: [] for name in names}
    handlers = {}
    for name in names:

        def capture(sender, *, event, name=name):
            received[name].append(event)
            if observe is not None:
                observe(event)

        handlers[name] = capture
        getattr(signals, name).connect(capture, weak=False)
    try:
        yield received
    finally:
        for name, handler in handlers.items():
            getattr(signals, name).disconnect(handler)


def test_orga_reopen_race_preserves_completed_winner(
    site_app, admin_app, make_tournament, actors, monkeypatch
):
    orga, _, admin = actors
    tournament = make_tournament()
    tournament_service.change_status(
        tournament.id, TournamentStatus.PAUSED, admin.id
    ).unwrap()
    winner = tournament_repository.get_participants_for_tournament(
        tournament.id
    )[0].id
    db.session.rollback()
    loaded, release = threading.Event(), threading.Event()
    strong = {}
    real_read = site_views._get_tournament_or_404

    def read(tournament_id):
        snapshot = real_read(tournament_id)
        if _is_worker('stale') and snapshot.id == tournament.id:
            from flask import g

            assert g.user.id == orga.id
            assert not g.user.has_permission('lan_tournament.administrate')
            strong['row'] = db.session.get(DbTournament, tournament.id)
            assert snapshot.tournament_status is TournamentStatus.PAUSED
            loaded.set()
            assert release.wait(15)
            # Holding this reference prevents SQLAlchemy's weak identity-map
            # eviction from accidentally replacing the deliberately stale row.
            assert (
                strong['row'].tournament_status == TournamentStatus.PAUSED.name
            )
        return snapshot

    monkeypatch.setattr(site_views, '_get_tournament_or_404', read)
    with _workers(site_app) as spawn:
        route, _ = spawn(
            'stale',
            lambda: _post(
                site_app,
                orga,
                f'/lan-tournaments/orga/tournaments/{tournament.id}/resume',
                {'allow_completed_reopen': 'true'},
            ),
            release,
        )
        assert loaded.wait(15)

        def complete():
            tournament_service.change_status(
                tournament.id, TournamentStatus.ONGOING, admin.id
            ).unwrap()
            tournament_repository.set_tournament_winner(
                tournament.id,
                winner_team_id=None,
                winner_participant_id=winner,
            ).unwrap()
            return tournament_service.change_status(
                tournament.id, TournamentStatus.COMPLETED, admin.id
            )

        peer, _ = spawn('complete', complete)
        assert peer.result(timeout=15).is_ok()
        release.set()
        code, _, flashes = route.result(timeout=15)
    assert code == 302
    found, entries = _state(admin_app, tournament)
    assert found.tournament_status is TournamentStatus.COMPLETED
    assert found.winner_participant_id == winner
    assert not [
        e
        for e in entries
        if e.event_type == 'tournament-status-changed'
        and e.data['old_status'] == TournamentStatus.COMPLETED.name
    ]
    assert any(DENIED in message for message in flashes)


def test_admin_reopen_clears_winner_atomically(
    admin_app, make_tournament, actors
):
    orga, _, admin = actors
    tournament = make_tournament()
    winner = tournament_repository.get_participants_for_tournament(
        tournament.id
    )[0].id
    tournament_repository.set_tournament_winner(
        tournament.id,
        winner_team_id=None,
        winner_participant_id=winner,
    ).unwrap()
    tournament_service.change_status(
        tournament.id, TournamentStatus.COMPLETED, admin.id
    ).unwrap()
    url = f'/lan-tournaments/tournaments/{tournament.id}/reopen'
    # Real authorization negative control; the assigned Orga is not an admin.
    assert _post(admin_app, orga, url)[0] == 403
    assert _state(admin_app, tournament)[0].winner_participant_id == winner

    def committed_reopen(event):
        persisted, audit = _state(admin_app, tournament)
        assert persisted.tournament_status is TournamentStatus.ONGOING
        assert persisted.winner_participant_id is None
        assert persisted.winner_team_id is None
        assert (
            len(
                [
                    e
                    for e in audit
                    if e.event_type == 'tournament-status-changed'
                    and e.data['old_status'] == TournamentStatus.COMPLETED.name
                ]
            )
            == 1
        )

    with _events(
        'tournament_status_changed', observe=committed_reopen
    ) as events:
        code, _, _ = _post(admin_app, admin, url)
    assert code == 302
    found, entries = _state(admin_app, tournament)
    assert found.tournament_status is TournamentStatus.ONGOING
    assert found.winner_participant_id is None
    assert found.winner_team_id is None
    reopened = [
        e
        for e in entries
        if e.event_type == 'tournament-status-changed'
        and e.data['old_status'] == TournamentStatus.COMPLETED.name
    ]
    assert len(reopened) == 1
    assert reopened[0].initiator_id == admin.id
    assert reopened[0].data['new_status'] == TournamentStatus.ONGOING.name
    assert len(events['tournament_status_changed']) == 1


@pytest.mark.parametrize(
    'mode',
    [EliminationMode.SINGLE_ELIMINATION, EliminationMode.DOUBLE_ELIMINATION],
)
def test_ffa_submission_cannot_confirm_peer_placements(
    site_app, admin_app, make_tournament, actors, monkeypatch, mode
):
    orga, _, _ = actors
    tournament = make_tournament(mode)
    match, submitted = _final(tournament)
    reversed_placements = {cid: 3 - place for cid, place in submitted.items()}
    staged, release = threading.Event(), threading.Event()
    real_impl = tournament_match_service._confirm_ffa_match_impl
    real_public = tournament_match_service.confirm_ffa_match
    paused = []

    def pause():
        if _is_worker('submit') and not paused:
            # A split-commit mutant may have returned its connection to the
            # pool. Pin a live read-only transaction at this seam and record
            # its current PID, so the peer cannot reuse an obsolete holder PID.
            _local.state['pid'] = _limits()
            paused.append(True)
            staged.set()
            assert release.wait(15)

    def confirm_impl(*args, **kwargs):
        pause()
        return real_impl(*args, **kwargs)

    def confirm_public(*args, **kwargs):
        # Also catches the legacy public API boundary in the split-commit
        # scratch mutant; fixed production never calls this public wrapper.
        pause()
        return real_public(*args, **kwargs)

    monkeypatch.setattr(
        tournament_match_service, '_confirm_ffa_match_impl', confirm_impl
    )
    monkeypatch.setattr(
        tournament_match_service, 'confirm_ffa_match', confirm_public
    )

    def committed_result(event):
        persisted, confirmed, placements, audit = _state(
            admin_app, tournament, match
        )
        assert persisted.tournament_status is TournamentStatus.COMPLETED
        assert confirmed.confirmed_by == orga.id
        assert {
            cid: values[0] for cid, values in placements.items()
        } == submitted
        assert (
            len(
                [
                    e
                    for e in audit
                    if e.event_type == 'ffa-match-confirmed'
                    and e.data['match_id'] == str(match.id)
                ]
            )
            == 1
        )

    with (
        _events(
            'match_confirmed', 'tournament_completed', observe=committed_result
        ) as events,
        _workers(site_app) as spawn,
    ):
        route, holder = spawn(
            'submit',
            lambda: _post(
                site_app,
                orga,
                f'/lan-tournaments/orga/matches/{match.id}/submit_ffa_result',
                {
                    f'placement_{cid}': str(place)
                    for cid, place in submitted.items()
                },
            ),
            release,
        )
        assert staged.wait(15)
        peer, waiter = spawn(
            'substitute',
            lambda: tournament_match_service.set_ffa_placements(
                match.id, reversed_placements
            ),
        )
        blocked = _blocked_or_finished(peer, waiter, holder)
        release.set()
        code, _, flashes = route.result(timeout=15)
        peer_result = peer.result(timeout=15)
    assert code == 302
    assert peer_result.is_err(), (
        'peer replaced the submitted placements before confirmation'
    )
    assert (
        peer_result.unwrap_err()
        == 'Cannot modify placements of a confirmed match.'
    )
    assert blocked, (
        'the actual placement contender never waited on the result owner'
    )
    assert 'FFA match has been confirmed.' in flashes
    found, confirmed, placements, entries = _state(admin_app, tournament, match)
    expected = {
        cid: (place, 5 if place == 1 else 3, None)
        for cid, place in submitted.items()
    }
    assert placements == expected
    assert confirmed.confirmed_by == orga.id
    winner = next(cid for cid, place in submitted.items() if place == 1)
    assert found.tournament_status is TournamentStatus.COMPLETED
    assert str(found.winner_participant_id) == winner
    audits = [
        e
        for e in entries
        if e.event_type == 'ffa-match-confirmed'
        and e.data['match_id'] == str(match.id)
    ]
    assert len(audits) == 1
    assert audits[0].initiator_id == orga.id
    assert audits[0].data['placements'] == {
        cid: {'placement': place, 'points': 5 if place == 1 else 3}
        for cid, place in submitted.items()
    }
    assert (
        len(events['match_confirmed'])
        == len(events['tournament_completed'])
        == 1
    )
    assert str(events['match_confirmed'][0].winner_participant_id) == winner
    assert (
        str(events['tournament_completed'][0].winner_participant_id) == winner
    )


@pytest.mark.parametrize('failure', ['placements', 'audit', 'database'])
def test_ffa_submission_failure_rolls_back_original_state(
    admin_app, make_tournament, actors, monkeypatch, failure
):
    orga, _, _ = actors
    tournament = make_tournament(EliminationMode.SINGLE_ELIMINATION)
    match, submitted = _final(tournament)
    original = {cid: 3 - place for cid, place in submitted.items()}
    tournament_match_service.set_ffa_placements(match.id, original).unwrap()
    before = _state(admin_app, tournament, match)
    real_confirm = tournament_match_service._confirm_ffa_match_impl

    def fail(*args, **kwargs):
        if failure != 'placements':
            assert real_confirm(*args, **kwargs).is_ok()
            staged = tournament_repository.get_tournament(
                tournament.id, fresh=True
            )
            assert staged.tournament_status is TournamentStatus.COMPLETED
            assert (
                tournament_repository.get_match(match.id).confirmed_by
                == orga.id
            )
            assert any(
                e.event_type == 'ffa-match-confirmed'
                for e in tournament_log_service.get_entries_for_tournament(
                    tournament.id
                )
            )
        if failure == 'database':
            db.session.execute(text('SELECT 1/0'))
        raise RuntimeError('injected precommit failure')

    monkeypatch.setattr(
        tournament_match_service, '_confirm_ffa_match_impl', fail
    )
    with _events('match_confirmed', 'tournament_completed') as events:
        with pytest.raises(
            DataError if failure == 'database' else RuntimeError
        ):
            tournament_match_service.set_and_confirm_ffa_match(
                match.id, submitted, orga.id
            )
    # Same session, before fixture cleanup: detect missing rollback, even when
    # a fresh connection alone could not see an uncommitted poisoned result.
    assert db.session.scalar(text('SELECT 1')) == 1
    assert tournament_repository.get_match(match.id).confirmed_by is None
    db.session.commit()
    after = _state(admin_app, tournament, match)
    assert after == before
    assert events == {'match_confirmed': [], 'tournament_completed': []}


def _submit(party, proposer, name):
    return tournament_request_service.submit_request(
        party.id,
        proposer.id,
        party_capacity=None,
        name=name,
        game='Security game',
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        team_size=1,
        participant_limit=8,
        preferred_start_time=datetime(2026, 10, 24, 18, tzinfo=UTC),
        preferred_end_time=datetime(2026, 10, 24, 22, tzinfo=UTC),
        description='Security regression request.',
    )


def _seed_quota(party, proposer):
    seeds = [
        _submit(party, proposer, f'Seed {i} {uuid4()}').unwrap()[0]
        for i in range(2)
    ]
    assert (
        tournament_request_repository.count_open_requests_for_proposer(
            party.id, proposer.id
        )
        == 2
    )
    db.session.rollback()
    return seeds


def test_quota_parallel_submissions_stop_at_three(
    admin_app, quota_scope, monkeypatch
):
    party, proposer = quota_scope
    seeds = _seed_quota(party, proposer)
    acquired, release = threading.Event(), threading.Event()
    real_lock = tournament_request_repository.lock_request_quota_for_update

    def lock(*args):
        _limits()
        real_lock(*args)
        if _is_worker('holder'):
            acquired.set()
            assert release.wait(15)

    monkeypatch.setattr(
        tournament_request_repository, 'lock_request_quota_for_update', lock
    )
    with (
        _events('tournament_request_submitted') as events,
        _workers(admin_app) as spawn,
    ):
        first, holder = spawn(
            'holder',
            lambda: _submit(party, proposer, f'Race A {uuid4()}'),
            release,
        )
        assert acquired.wait(15)
        second, waiter = spawn(
            'waiter', lambda: _submit(party, proposer, f'Race B {uuid4()}')
        )
        blocked = _blocked_or_finished(second, waiter, holder)
        release.set()
        results = [first.result(timeout=15), second.result(timeout=15)]
    assert sum(result.is_ok() for result in results) == 1
    assert [result.unwrap_err() for result in results if result.is_err()] == [
        QUOTA
    ]
    assert blocked
    assert (
        tournament_request_repository.count_open_requests_for_proposer(
            party.id, proposer.id
        )
        == 3
    )
    successful = next(
        result.unwrap()[0] for result in results if result.is_ok()
    )
    assert successful.id not in {seed.id for seed in seeds}
    history = tournament_request_service.get_request_history(successful.id)
    assert len(history) == 1
    assert history[0].event_type == 'tournament-request-submitted'
    assert history[0].initiator_id == proposer.id
    assert len(events['tournament_request_submitted']) == 1
    assert events['tournament_request_submitted'][0].request_id == successful.id


def test_quota_retry_rechecks_after_rollback(
    admin_app, quota_scope, monkeypatch
):
    party, proposer = quota_scope
    seeds = _seed_quota(party, proposer)
    retry, release = threading.Event(), threading.Event()
    real_lock = tournament_request_repository.lock_request_quota_for_update
    real_number = tournament_request_repository.get_next_number_for_party
    locks, numbers, counts = [], [], []
    real_count = tournament_request_repository.count_open_requests_for_proposer

    def lock(*args):
        _limits()
        if _is_worker('retry'):
            locks.append(db.session.scalar(text('SELECT txid_current()')))
            if len(locks) == 2:
                retry.set()
                assert release.wait(15)
        real_lock(*args)

    def number(party_id):
        if _is_worker('retry'):
            numbers.append(True)
            if len(numbers) == 1:
                return seeds[0].number  # actual PostgreSQL unique violation
        return real_number(party_id)

    def count(*args):
        value = real_count(*args)
        if _is_worker('retry'):
            counts.append(value)
        return value

    monkeypatch.setattr(
        tournament_request_repository, 'lock_request_quota_for_update', lock
    )
    monkeypatch.setattr(
        tournament_request_repository, 'get_next_number_for_party', number
    )
    monkeypatch.setattr(
        tournament_request_repository, 'count_open_requests_for_proposer', count
    )
    with (
        _events('tournament_request_submitted') as events,
        _workers(admin_app) as spawn,
    ):
        first, _ = spawn(
            'retry',
            lambda: _submit(party, proposer, f'Retry {uuid4()}'),
            release,
        )
        assert retry.wait(15)
        peer, _ = spawn(
            'fill', lambda: _submit(party, proposer, f'Fill {uuid4()}')
        )
        assert peer.result(timeout=15).is_ok()
        release.set()
        result = first.result(timeout=15)
    assert result.is_err(), (
        'retry inserted after another transaction filled the quota'
    )
    assert result.unwrap_err() == QUOTA
    assert counts == [2, 3]
    assert len(locks) == 2 and locks[0] != locks[1]
    assert len(numbers) == 1
    assert (
        tournament_request_repository.count_open_requests_for_proposer(
            party.id, proposer.id
        )
        == 3
    )
    assert len(events['tournament_request_submitted']) == 1


@pytest.mark.parametrize('different', ['proposer', 'party'])
def test_quota_lock_does_not_block_other_keys(
    admin_app, quota_scope, make_user, make_party, make_brand, different
):
    party, proposer = quota_scope
    suffix = uuid4().hex
    other_party = (
        make_party(
            make_brand(f'other-{suffix}', f'Other {suffix}'),
            PartyID(f'other-{suffix}'),
            f'Other party {suffix}',
        )
        if different == 'party'
        else party
    )
    other_proposer = (
        make_user(f'Other{suffix[:12]}')
        if different == 'proposer'
        else proposer
    )
    held, release = threading.Event(), threading.Event()

    def hold():
        tournament_request_repository.lock_request_quota_for_update(
            party.id, proposer.id
        )
        held.set()
        assert release.wait(15)

    def acquire_other():
        # NOWAIT-equivalent advisory acquisition makes independence observable,
        # not a scheduler/timing guess. Then exercise the real public lock API.
        key = tournament_request_repository._request_quota_lock_key(
            other_party.id, other_proposer.id
        )
        assert db.session.scalar(
            text('SELECT pg_try_advisory_xact_lock(:key)'), {'key': key}
        )
        tournament_request_repository.lock_request_quota_for_update(
            other_party.id, other_proposer.id
        )
        return True

    with _workers(admin_app) as spawn:
        _, holder = spawn('hold-key', hold, release)
        assert held.wait(15)
        peer, waiter = spawn('other-key', acquire_other)
        assert waiter['pid'] != holder['pid']
        assert waiter['session'] is not holder['session']
        assert peer.result(timeout=15)
        release.set()


def test_invalid_date_precheck_returns_field_errors(
    admin_app, party, actors, monkeypatch
):
    _, _, admin = actors
    # The original overflow is an HTTP 500 assertion failure in sensitivity
    # runs, not a setup/import error or an exception escaping the test client.
    monkeypatch.setitem(admin_app.config, 'PROPAGATE_EXCEPTIONS', False)
    # Application was configured for Berlin before Babel initialization.
    with admin_app.test_request_context():
        assert str(get_timezone()) == 'Europe/Berlin'
    watched = [
        'lan_tournaments',
        'lan_tournament_images',
        'lan_tournament_log_entries',
        'lan_tournament_requests',
    ]

    def counts():
        with db.engine.connect() as connection:
            return {
                table: connection.scalar(text(f'SELECT count(*) FROM {table}'))  # noqa: S608 -- fixed test-owned table names
                for table in watched
            }

    before = counts()
    statements = []
    from sqlalchemy import event

    def record(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement.lstrip().split()[0].upper())

    event.listen(db.engine, 'before_cursor_execute', record)
    try:
        with http_client(admin_app, user_id=admin.id) as client:
            response = client.post(
                f'/lan-tournaments/for_party/{party.id}/create/validate',
                data={
                    'name': f'Invalid date {uuid4()}',
                    'contestant_type': 'SOLO',
                    'game_format': 'ONE_V_ONE',
                    'elimination_mode': 'SINGLE_ELIMINATION',
                    'max_players': '16',
                    'start_time': '0001-01-01T00:00',
                },
                headers={'Accept-Language': 'en'},
            )
    finally:
        event.remove(db.engine, 'before_cursor_execute', record)
    assert response.status_code == 200
    assert response.is_json
    body = response.get_json()
    assert body['ok'] is False
    assert set(body['errors']) == {'start_time'}
    assert body['errors']['start_time']
    assert (
        '2000' in body['errors']['start_time'][0]
        and '2100' in body['errors']['start_time'][0]
    )
    assert body['first_error_step'] == 0
    assert body['refusal'] is None
    assert not {'INSERT', 'UPDATE', 'DELETE'} & set(statements)
    assert counts() == before
