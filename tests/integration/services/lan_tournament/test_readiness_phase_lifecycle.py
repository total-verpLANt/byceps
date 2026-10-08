"""Real PostgreSQL phase generation, retirement and transaction evidence."""

from datetime import datetime, timedelta, UTC
from functools import partial

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from byceps.database import db
from byceps.services.lan_tournament import (
    tournament_match_service as engine,
    tournament_qualification_service as qualification,
    tournament_readiness_service as readiness,
    tournament_repository as repo,
    tournament_seeding_service as seeding,
    tournament_service,
)
from byceps.services.lan_tournament.dbmodels.match import DbTournamentMatch
from byceps.services.lan_tournament.dbmodels.match_readiness import DbMatchInvitation, DbMatchPairing
from byceps.services.lan_tournament.models.playoff import PlayoffReleaseMode
from byceps.services.lan_tournament.models import ContestantType
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.elimination_mode import EliminationMode
from byceps.services.lan_tournament.models.score_ordering import ScoreOrdering
from byceps.services.lan_tournament.models.tournament_participant import TournamentParticipant, TournamentParticipantID
from byceps.services.lan_tournament.models.tournament_match import MatchSide
from byceps.services.lan_tournament.models.tournament_status import TournamentStatus
from byceps.services.party.models import PartyID
from byceps.util.uuid import uuid7

from . import test_generate_from_seeding as initial
from . import test_playoff_release as playoffs
from . import test_qualification_matrix as matrix


@pytest.fixture(scope='module')
def party(make_party, make_brand):
    slug = uuid7().hex[-12:]
    brand = make_brand(f'phase11-{slug}', 'Phase 11')
    return make_party(brand, PartyID(f'phase11-{slug}'), 'Phase 11')


@pytest.fixture(scope='module')
def users(make_user):
    return [make_user(f'Phase11-{uuid7().hex[-12:]}') for _ in range(8)]


@pytest.fixture
def initial_factory(party, users):
    def make(mode=initial.SE, participants=8, **kwargs):
        result = tournament_service.create_tournament(
            party.id, f'Phase 11 {uuid7()}', contestant_type=ContestantType.SOLO,
            game_format=mode[0], elimination_mode=mode[1],
            tournament_status=TournamentStatus.REGISTRATION_CLOSED, **kwargs,
        )
        assert result.is_ok(), result.unwrap_err()
        tournament, _ = result.unwrap()
        ids = []
        for user in users[:participants]:
            pid = TournamentParticipantID(uuid7())
            repo.create_participant(TournamentParticipant(
                id=pid, tournament_id=tournament.id, user_id=user.id,
                created_at=datetime.now(UTC), team_id=None, substitute_player=False,
            ))
            ids.append(pid)
        db.session.commit()
        return tournament, ids

    yield make
    # All rows have unique IDs/names and belong to this isolated session DB.
    # Do not attempt terminal deletion or clean any sibling fixture's data.
    db.session.rollback()


@pytest.fixture
def make_tournament(initial_factory, users):
    def make(*, release_mode=PlayoffReleaseMode.MANUAL):
        tournament, _ = initial_factory(
            initial.RR, playoff_game_format=GameFormat.ONE_V_ONE,
            playoff_elimination_mode=EliminationMode.SINGLE_ELIMINATION,
            playoff_group_count=2, playoff_qualifiers_per_group=2,
            playoff_release_mode=release_mode,
        )
        assert engine.generate_round_robin_bracket(tournament.id).is_ok()
        assert tournament_service.change_status(tournament.id, TournamentStatus.ONGOING, users[0].id).is_ok()
        return tournament

    return make


def _pairs(tournament_id, phase):
    matches = [m for m in repo.get_matches_for_tournament(tournament_id) if m.phase == phase]
    return {m.id: repo.get_match_pairing(m.id) for m in matches}


def _assert_pairings(tournament_id, phase):
    pairs = _pairs(tournament_id, phase)
    for match_id, pair in pairs.items():
        match = repo.get_match(match_id)
        contestants = repo.get_contestants_for_match(match_id)
        real_ids = {c.participant_id for c in contestants if c.participant_id is not None}
        if len(real_ids) != 2:
            assert pair is None
            continue
        assert pair is not None and pair.ended_at is None
        assert {pair.side_a.id, pair.side_b.id} == real_ids
        assert pair.generation == match.pairing_generation
        assert pair.started_at is not None
        assert match.occupied_since is not None
        assert pair.started_at >= match.occupied_since
        assert match.ready_at_a is match.ready_at_b is None
        assert match.ready_by_a is match.ready_by_b is None
    return pairs


@pytest.mark.parametrize('automatic', [False, True])
def test_generation_and_release_effects_are_post_commit(make_tournament, users, monkeypatch, automatic):
    mode = PlayoffReleaseMode.AUTOMATIC if automatic else PlayoffReleaseMode.MANUAL
    tournament = make_tournament(release_mode=mode)
    phase_one = _pairs(tournament.id, 1)
    observed = []
    real_dispatch = engine.dispatch_generation_events

    def dispatch(tournament_id, outcome):
        with Session(db.engine) as independent:
            matches = independent.scalars(select(DbTournamentMatch).where(
                DbTournamentMatch.tournament_id == tournament_id,
                DbTournamentMatch.phase == 2,
            )).all()
            assert len(matches) == outcome.count
            assert any(m.pairing_id is not None for m in matches)
            observed.append(outcome.count)
        real_dispatch(tournament_id, outcome)

    monkeypatch.setattr(engine, 'dispatch_generation_events', dispatch)
    playoffs._play_groups(tournament, users[0])
    if not automatic:
        result = playoffs._release(tournament, users[0])
        assert result.is_ok(), result.unwrap_err()
    assert observed and all(n > 0 for n in observed)
    pairs = _assert_pairings(tournament.id, 2)
    assert any(pairs.values())
    assert _pairs(tournament.id, 1) == phase_one


@pytest.mark.parametrize('status', ['pending', 'dispatching', 'queued', 'failed', 'accepted', 'sending', 'delivery_unknown'])
def test_release_unrelease_closes_phase_two_work(make_tournament, users, monkeypatch, status):
    tournament = make_tournament()
    playoffs._play_groups(tournament, users[0])
    assert playoffs._release(tournament, users[0]).is_ok()
    phase_one = _pairs(tournament.id, 1)
    old_pairs = _assert_pairings(tournament.id, 2)
    match_id, old_pair = next((mid, p) for mid, p in old_pairs.items() if p is not None)
    repo.get_tournament_for_update(tournament.id)
    match = repo.get_match_for_update(match_id)
    now = datetime.now(UTC).replace(tzinfo=None)
    repo.set_side_ready_flush(match_id, MatchSide.A, now, users[0].id)
    repo.set_side_ready_flush(match_id, MatchSide.B, now, users[1].id)
    token = uuid7() if status in {'dispatching', 'queued', 'sending'} else None
    # Existing notification handlers create recipient work after release. Seed
    # protocol outcomes explicitly; this is not evidence of actual SMTP delivery.
    row = db.session.scalars(select(DbMatchInvitation).where(
        DbMatchInvitation.match_id == match_id,
    )).first()
    assert row is not None
    invitation_id = row.id
    row.status = status
    row.expected_readiness_revision = match.readiness_revision
    row.dispatch_token = token
    row.lease_until = now + timedelta(minutes=2) if token else None
    row.accepted_at = now if status == 'accepted' else None
    row.last_error = None
    db.session.commit()
    real_delete = repo.delete_match_flush
    checked = []

    def delete(mid, **kwargs):
        if mid == match_id:
            assert repo.get_match_pairing(mid) is None
            assert repo.get_match_pairing_history(mid)[-1].ended_at is not None
            work = repo.get_match_invitation(invitation_id)
            expected = status if status in {'accepted', 'sending', 'delivery_unknown'} else 'suppressed'
            assert work.status.value == expected
            checked.append(mid)
        real_delete(mid, **kwargs)

    monkeypatch.setattr(repo, 'delete_match_flush', delete)
    result = qualification.unrelease_playoffs(tournament.id, reason='New playoff draw', initiator_id=users[0].id)
    assert result.is_ok(), result.unwrap_err()
    assert checked == [match_id]
    with Session(db.engine) as independent:
        assert independent.get(DbTournamentMatch, match_id) is None
        historical = independent.get(DbMatchPairing, old_pair.id)
        assert historical.ended_at is not None
        work = independent.get(DbMatchInvitation, invitation_id)
        if status in {'accepted', 'sending', 'delivery_unknown'}:
            assert work.status == status and work.dispatch_token == token
        else:
            assert work.status == 'suppressed' and work.last_error == 'pairing_retired'
            assert work.dispatch_token is work.lease_until is None
    assert playoffs._release(tournament, users[0]).is_ok()
    new_pairs = _assert_pairings(tournament.id, 2)
    assert set(new_pairs).isdisjoint(old_pairs)
    assert {p.id for p in new_pairs.values() if p}.isdisjoint({p.id for p in old_pairs.values() if p})
    assert _pairs(tournament.id, 1) == phase_one


@pytest.mark.parametrize('operation', ['release', 'unrelease', 'generate'])
def test_audit_failure_rolls_back_phase_writes(make_tournament, initial_factory, users, monkeypatch, operation):
    if operation == 'generate':
        tournament, _ = initial_factory()
        board = initial._board(tournament)
        call = partial(initial._generate, tournament, board, users[0])
        audit_module, audit_name = seeding, 'create_log_entry'
    else:
        tournament = make_tournament()
        playoffs._play_groups(tournament, users[0])
        board = seeding.ensure_playoff_draft(tournament.id).unwrap()
        if operation == 'release':
            call = partial(qualification.release_playoffs, tournament.id, expected_version=board.version, initiator_id=users[0].id)
        else:
            assert playoffs._release(tournament, users[0]).is_ok()
            call = partial(qualification.unrelease_playoffs, tournament.id, reason='New draw', initiator_id=users[0].id)
        audit_module, audit_name = qualification.tournament_log_service, 'create_log_entry'
    before = _pairs(tournament.id, 1), _pairs(tournament.id, 2)
    events = []
    monkeypatch.setattr(engine, 'dispatch_generation_events', lambda *args: events.append(args))

    def fail(*args, **kwargs):
        assert kwargs['commit'] is False
        raise RuntimeError('audit failed')

    monkeypatch.setattr(audit_module, audit_name, fail)
    with pytest.raises(RuntimeError, match='audit failed'):
        call()
    assert (_pairs(tournament.id, 1), _pairs(tournament.id, 2)) == before
    assert not events
    with Session(db.engine) as independent:
        persisted = independent.scalars(select(DbMatchPairing).where(DbMatchPairing.tournament_id == tournament.id)).all()
        assert {p.id for p in persisted} == {p.id for pairs in before for p in pairs.values() if p}


def test_regeneration_retires_old_claims_and_preserves_unchanged_pairings(initial_factory, users):
    tournament, _ = initial_factory()
    board = initial._board(tournament)
    assert initial._generate(tournament, board, users[0]).is_ok()
    old_pairs = _assert_pairings(tournament.id, 1)
    old = next(p for p in old_pairs.values() if p)
    repo.set_side_ready_flush(old.match_id, MatchSide.A, datetime.now(UTC), users[0].id)
    db.session.commit()
    snapshot = repo.get_match(old.match_id)
    assert initial._generate(tournament, initial._board(tournament), users[0]).unwrap() == seeding.GENERATION_UNCHANGED
    assert repo.get_match(old.match_id) == snapshot
    assert _pairs(tournament.id, 1) == old_pairs
    board = initial._action(tournament, initial._board(tournament), seeding.Swap(0, 1), users[0])
    assert initial._generate(tournament, board, users[0]).is_ok()
    new_pairs = _assert_pairings(tournament.id, 1)
    assert set(new_pairs).isdisjoint(old_pairs)
    assert all(p.ended_at is not None for p in repo.get_match_pairing_history(old.match_id))


def test_ffa_remains_unavailable(initial_factory, users):
    tournament, _ = initial_factory(initial.FFA, **initial.FFA_KWARGS)
    assert initial._generate(tournament, initial._board(tournament), users[0]).is_ok()
    assert repo.set_tournament_status_flush(tournament.id, TournamentStatus.ONGOING).is_ok()
    db.session.commit()
    for match in repo.get_matches_for_tournament(tournament.id):
        assert repo.get_match_pairing(match.id) is None
        result = readiness.claim_ready_flush(match.id, MatchSide.A, users[0].id,
            expected_pairing_generation=match.pairing_generation,
            expected_readiness_revision=match.readiness_revision)
        assert result.is_err() and result.unwrap_err() == 'readiness_format_unsupported'
        db.session.rollback()


def test_two_side_playoff_uses_effective_format(make_tournament, users):
    tournament = make_tournament()
    playoffs._play_groups(tournament, users[0])
    assert playoffs._release(tournament, users[0]).is_ok()
    pair = next(p for p in _pairs(tournament.id, 2).values() if p)
    facts = readiness._locked_facts(pair.match_id).unwrap()
    assert facts[1].phase == 2
    assert facts[-1] is True
    assert readiness._projection(facts).supports_readiness
    assert readiness._projection(facts).pairing_valid
    db.session.rollback()


@pytest.mark.parametrize('known', [False, True])
def test_same_pair_refresh_preserves_original_timing_honestly(make_tournament, users, known):
    tournament = make_tournament()
    pair = next(p for p in _pairs(tournament.id, 1).values() if p)
    repo.get_tournament_for_update(tournament.id)
    repo.get_match_for_update(pair.match_id)
    row = db.session.get(DbTournamentMatch, pair.match_id)
    historical = db.session.get(DbMatchPairing, pair.id)
    original = row.occupied_since - timedelta(days=1) if known else None
    row.occupied_since = original
    if not known:
        historical.started_at = None
    db.session.commit()
    before = repo.get_match_pairing(pair.match_id)
    result = readiness.refresh_pairing_and_invitations_flush(pair.match_id, occurred_at=datetime.now(UTC))
    assert result.is_ok(), result.unwrap_err()
    db.session.commit()
    assert repo.get_match_pairing(pair.match_id) == before
    assert repo.get_match(pair.match_id).occupied_since == original
    facts = readiness._locked_facts(pair.match_id).unwrap()
    projection = readiness._projection(facts)
    assert projection.original_occupied_since == original
    assert projection.pairing_started_at == before.started_at
    db.session.rollback()


def test_generation_reads_current_status_after_lock(initial_factory, users):
    tournament, _ = initial_factory()
    board = initial._board(tournament)
    from byceps.services.lan_tournament.dbmodels.tournament import DbTournament
    cached = db.session.get(DbTournament, tournament.id)
    assert cached.tournament_status == 'REGISTRATION_CLOSED'
    with Session(db.engine) as independent:
        row = independent.get(DbTournament, tournament.id)
        row.tournament_status = 'CANCELLED'
        independent.commit()
    assert cached.tournament_status == 'REGISTRATION_CLOSED'
    result = initial._generate(tournament, board, users[0])
    assert result.is_err() and result.unwrap_err() == seeding.ERR_LOCKED
    assert not _pairs(tournament.id, 1)


def test_ffa_playoff_remains_unavailable_with_highscore_base(initial_factory, users):
    tournament, _ = initial_factory(
        (GameFormat.HIGHSCORE, EliminationMode.NONE),
        score_ordering=ScoreOrdering.HIGHER_IS_BETTER,
        playoff_game_format=GameFormat.FREE_FOR_ALL,
        playoff_elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        playoff_qualifier_count=4, playoff_release_mode=PlayoffReleaseMode.MANUAL,
        point_table=[5, 3, 2, 1], group_size_min=3, group_size_max=4,
        advancement_count=2,
    )
    assert tournament_service.change_status(tournament.id, TournamentStatus.ONGOING, users[0].id).is_ok()
    played = matrix._play_highscore(tournament, users, 'clear')
    played.finish()
    assert playoffs._release(tournament, users[0]).is_ok()
    matches = playoffs._phase_two(tournament)
    assert matches
    for match in matches:
        assert repo.get_match_pairing(match.id) is None
        result = readiness.claim_ready_flush(match.id, MatchSide.A, users[0].id,
            expected_pairing_generation=match.pairing_generation,
            expected_readiness_revision=match.readiness_revision)
        assert result.is_err() and result.unwrap_err() == 'readiness_format_unsupported'
        db.session.rollback()
