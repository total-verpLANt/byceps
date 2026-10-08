from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from byceps.database import db
from byceps.services.lan_tournament import tournament_repository as repo
from byceps.services.lan_tournament.dbmodels.match import DbTournamentMatch
from byceps.services.lan_tournament.dbmodels.match_contestant import (
    DbTournamentMatchToContestant,
)
from byceps.services.lan_tournament.dbmodels.participant import (
    DbTournamentParticipant,
)
from byceps.services.lan_tournament.dbmodels.team import DbTournamentTeam
from byceps.services.lan_tournament.dbmodels.tournament import DbTournament
from byceps.services.lan_tournament.models.operational_timing import (
    MatchDueEpisode,
    MatchDueEpisodeID,
    MatchEscalationAcknowledgement,
    MatchEscalationAcknowledgementID,
)
from byceps.services.lan_tournament.models.tournament import TournamentID
from byceps.services.lan_tournament.models.tournament_match import (
    MatchSide,
    TournamentMatch,
    TournamentMatchID,
)
from byceps.services.lan_tournament.models.tournament_match_comment import (
    TournamentMatchComment,
    TournamentMatchCommentID,
)
from byceps.services.lan_tournament.models.tournament_match_to_contestant import (
    TournamentMatchToContestant,
)
from byceps.services.party.models import PartyID
from byceps.util.uuid import uuid7


PARTY_ID = PartyID('f03-last-changed-repository')

BASE = datetime(2026, 10, 7, 12, 0, 0)
LATER = BASE + timedelta(hours=1)
EARLIER = BASE - timedelta(hours=1)


@pytest.fixture(scope='module')
def party(make_party, brand):
    return make_party(brand, PARTY_ID, 'F03 last changed repository')


@pytest.fixture(scope='module')
def users(make_user):
    return [make_user(f'LastChanged{i}') for i in range(3)]


@pytest.fixture(autouse=True)
def _session(party):
    """Provide the app context, and leave no open transaction behind."""
    yield
    db.session.rollback()


@pytest.fixture
def world(party, users):
    tournament = DbTournament(
        uuid7(),
        party.id,
        f'Last change {uuid7()}',
        BASE,
        game_format='ONE_V_ONE',
        tournament_status='ONGOING',
    )
    db.session.add(tournament)
    db.session.flush()
    participants = [
        DbTournamentParticipant(uuid7(), user.id, tournament.id, BASE)
        for user in users
    ]
    team = DbTournamentTeam(
        uuid7(), tournament.id, f'Team {uuid7()}', users[0].id, BASE
    )
    db.session.add_all([*participants, team])
    db.session.commit()
    return SimpleNamespace(
        tournament_id=TournamentID(tournament.id),
        user_ids=[user.id for user in users],
        participant_ids=[participant.id for participant in participants],
        team_id=team.id,
    )


# -------------------------------------------------------------------- #
# helpers
# -------------------------------------------------------------------- #


def _match(world, *, confirmed_by=None, **facts) -> TournamentMatchID:
    """Create a match whose last change is `BASE`, written the plain way."""
    match_id = TournamentMatchID(uuid7())
    repo.create_match(
        TournamentMatch(
            id=match_id,
            tournament_id=world.tournament_id,
            group_order=None,
            match_order=1,
            round=1,
            next_match_id=None,
            confirmed_by=confirmed_by,
            created_at=BASE,
            **facts,
        ),
        changed_at=BASE,
    )
    return match_id


def _seat(match_id, *, participant_id=None, team_id=None, **facts):
    """Insert a contestant row without going through a repository writer."""
    row = DbTournamentMatchToContestant(
        uuid7(),
        match_id,
        BASE,
        participant_id=participant_id,
        team_id=team_id,
        **facts,
    )
    db.session.add(row)
    db.session.flush()
    return row.id


def _ready(match_id, side_column: str, user_id) -> None:
    row = db.session.get(DbTournamentMatch, match_id)
    setattr(row, f'ready_at_{side_column}', BASE)
    setattr(row, f'ready_by_{side_column}', user_id)
    db.session.flush()


def _read(sql: str, **params):
    """Read through a separate connection: only committed data is visible."""
    with db.engine.connect() as connection:
        return connection.execute(text(sql), params).all()


def _last_changed(match_id):
    [(value,)] = _read(
        'SELECT last_changed_at FROM lan_tournament_matches WHERE id = :id',
        id=match_id,
    )
    return value


def _fingerprint(match_id):
    """Return the committed domain facts of a match."""
    return (
        _read(
            'SELECT confirmed_by, ready_at_a, ready_by_a, ready_at_b, ready_by_b'
            ' FROM lan_tournament_matches WHERE id = :id',
            id=match_id,
        ),
        _read(
            'SELECT id, participant_id, team_id, score, placement, points'
            ' FROM lan_tournament_match_contestants'
            ' WHERE tournament_match_id = :id ORDER BY id',
            id=match_id,
        ),
    )


def _arrange(world, case):
    """Build a case, a control match and commit the starting state."""
    target, act = case(world)
    control = _match(world)
    db.session.commit()
    return target, control, act


# -------------------------------------------------------------------- #
# actual writes: each case returns (target match, action)
# -------------------------------------------------------------------- #


def _case_create_contestant(world):
    match_id = _match(world)
    contestant = TournamentMatchToContestant(
        id=uuid7(),
        tournament_match_id=match_id,
        team_id=None,
        participant_id=world.participant_ids[0],
        score=None,
        created_at=BASE,
    )
    return match_id, lambda: repo.create_match_contestant(
        contestant, changed_at=LATER
    )


def _case_confirm(world):
    match_id = _match(world)
    return match_id, lambda: repo.confirm_match(
        match_id, world.user_ids[0], changed_at=LATER
    )


def _case_confirm_by_another_user(world):
    match_id = _match(world, confirmed_by=world.user_ids[0])
    return match_id, lambda: repo.confirm_match(
        match_id, world.user_ids[1], changed_at=LATER
    )


def _case_unconfirm(world):
    match_id = _match(world, confirmed_by=world.user_ids[0])
    return match_id, lambda: repo.unconfirm_match(match_id, changed_at=LATER)


def _case_unconfirm_without_readiness_reset(world):
    match_id = _match(world, confirmed_by=world.user_ids[0])
    return match_id, lambda: repo.unconfirm_match(
        match_id, reset_readiness=False, changed_at=LATER
    )


def _case_single_score(world):
    match_id = _match(world)
    contestant_id = _seat(
        match_id, participant_id=world.participant_ids[0], score=1
    )
    return match_id, lambda: repo.update_contestant_score(
        contestant_id, 7, changed_at=LATER
    )


def _case_bulk_scores(world):
    match_id = _match(world)
    first = _seat(match_id, participant_id=world.participant_ids[0], score=1)
    second = _seat(match_id, participant_id=world.participant_ids[1], score=2)
    return match_id, lambda: repo.update_contestant_scores(
        {first: 1, second: 3}, changed_at=LATER
    )


def _case_first_score(world):
    match_id = _match(world)
    contestant_id = _seat(match_id, participant_id=world.participant_ids[0])
    return match_id, lambda: repo.update_contestant_scores(
        {contestant_id: 0}, changed_at=LATER
    )


def _case_clear_scores(world):
    match_id = _match(world)
    _seat(match_id, participant_id=world.participant_ids[0], score=4)
    _seat(match_id, participant_id=world.participant_ids[1])
    return match_id, lambda: repo.clear_contestant_scores(
        match_id, changed_at=LATER
    )


def _case_placements(world):
    match_id = _match(world)
    first = _seat(
        match_id, participant_id=world.participant_ids[0], placement=1, points=3
    )
    second = _seat(
        match_id, participant_id=world.participant_ids[1], placement=2, points=1
    )
    return match_id, lambda: repo.update_contestant_placement_and_points(
        {first: (1, 3), second: (2, 2)}, changed_at=LATER
    )


def _case_delete_contestant_from_match(world):
    match_id = _match(world)
    _seat(match_id, participant_id=world.participant_ids[0])
    return match_id, lambda: repo.delete_contestant_from_match(
        match_id, participant_id=world.participant_ids[0], changed_at=LATER
    )


def _case_delete_team_contestant_from_match(world):
    match_id = _match(world)
    _seat(match_id, team_id=world.team_id)
    return match_id, lambda: repo.delete_contestant_from_match(
        match_id, team_id=world.team_id, changed_at=LATER
    )


def _case_delete_match_contestant(world):
    match_id = _match(world)
    contestant_id = _seat(match_id, participant_id=world.participant_ids[0])
    return match_id, lambda: repo.delete_match_contestant_flush(
        contestant_id, changed_at=LATER
    )


def _case_delete_contestants_for_match(world):
    match_id = _match(world)
    _seat(match_id, participant_id=world.participant_ids[0])
    _seat(match_id, participant_id=world.participant_ids[1])
    return match_id, lambda: repo.delete_contestants_for_match_flush(
        match_id, changed_at=LATER
    )


def _case_remove_team_from_contestants(world):
    match_id = _match(world)
    _seat(match_id, team_id=world.team_id)
    _seat(match_id, participant_id=world.participant_ids[1])
    return match_id, lambda: repo.remove_team_from_contestants_flush(
        world.team_id, changed_at=LATER
    )


def _case_ready_claim(world, side: MatchSide):
    match_id = _match(world)
    return match_id, lambda: repo.set_side_ready_flush(
        match_id, side, LATER, world.user_ids[0], changed_at=LATER
    )


def _case_ready_claim_by_another_user(world, side: MatchSide):
    match_id = _match(world)
    _ready(match_id, side.name.lower(), world.user_ids[0])
    return match_id, lambda: repo.set_side_ready_flush(
        match_id, side, BASE, world.user_ids[1], changed_at=LATER
    )


def _case_ready_revoke(world, side: MatchSide):
    match_id = _match(world)
    _ready(match_id, side.name.lower(), world.user_ids[0])
    return match_id, lambda: repo.clear_side_ready_flush(
        match_id, side, changed_at=LATER
    )


# fmt: off
ACTUAL_WRITES = {
    'create_match_contestant': _case_create_contestant,
    'confirm_match': _case_confirm,
    'confirm_match_by_another_user': _case_confirm_by_another_user,
    'unconfirm_match': _case_unconfirm,
    'unconfirm_match_without_readiness_reset': _case_unconfirm_without_readiness_reset,
    'update_contestant_score': _case_single_score,
    'update_contestant_scores': _case_bulk_scores,
    'update_contestant_scores_first_score': _case_first_score,
    'clear_contestant_scores': _case_clear_scores,
    'update_contestant_placement_and_points': _case_placements,
    'delete_contestant_from_match_participant': _case_delete_contestant_from_match,
    'delete_contestant_from_match_team': _case_delete_team_contestant_from_match,
    'delete_match_contestant_flush': _case_delete_match_contestant,
    'delete_contestants_for_match_flush': _case_delete_contestants_for_match,
    'remove_team_from_contestants_flush': _case_remove_team_from_contestants,
    'set_side_ready_flush_a': lambda w: _case_ready_claim(w, MatchSide.A),
    'set_side_ready_flush_b': lambda w: _case_ready_claim(w, MatchSide.B),
    'set_side_ready_flush_replaced_claim_a': lambda w: _case_ready_claim_by_another_user(w, MatchSide.A),
    'set_side_ready_flush_replaced_claim_b': lambda w: _case_ready_claim_by_another_user(w, MatchSide.B),
    'clear_side_ready_flush_a': lambda w: _case_ready_revoke(w, MatchSide.A),
    'clear_side_ready_flush_b': lambda w: _case_ready_revoke(w, MatchSide.B),
}
# The legacy single-score writer still commits on its own (Issue 9).
COMMITS_ITSELF = {'update_contestant_score'}
# fmt: on


@pytest.mark.parametrize('name', list(ACTUAL_WRITES))
def test_actual_domain_writes_touch_last_changed(world, name):
    target, control, act = _arrange(world, ACTUAL_WRITES[name])
    before = _fingerprint(target)

    act()

    if name not in COMMITS_ITSELF:
        assert _last_changed(target) == BASE
        assert _fingerprint(target) == before
    repo.commit_session()

    assert _last_changed(target) == LATER
    assert _fingerprint(target) != before
    assert _last_changed(control) == BASE


# -------------------------------------------------------------------- #
# no-op writes
# -------------------------------------------------------------------- #


def _noop_single_score(world):
    match_id = _match(world)
    contestant_id = _seat(
        match_id, participant_id=world.participant_ids[0], score=5
    )
    return match_id, lambda: repo.update_contestant_score(
        contestant_id, 5, changed_at=LATER
    )


def _noop_bulk_scores(world):
    match_id = _match(world)
    first = _seat(match_id, participant_id=world.participant_ids[0], score=5)
    second = _seat(match_id, participant_id=world.participant_ids[1], score=3)
    return match_id, lambda: repo.update_contestant_scores(
        {first: 5, second: 3}, changed_at=LATER
    )


def _noop_placements(world):
    match_id = _match(world)
    first = _seat(
        match_id, participant_id=world.participant_ids[0], placement=1, points=3
    )
    second = _seat(
        match_id, participant_id=world.participant_ids[1], placement=2, points=1
    )
    return match_id, lambda: repo.update_contestant_placement_and_points(
        {first: (1, 3), second: (2, 1)}, changed_at=LATER
    )


def _noop_clear_scores(world):
    match_id = _match(world)
    _seat(match_id, participant_id=world.participant_ids[0])
    return match_id, lambda: repo.clear_contestant_scores(
        match_id, changed_at=LATER
    )


def _noop_confirm(world):
    match_id = _match(world, confirmed_by=world.user_ids[0])
    return match_id, lambda: repo.confirm_match(
        match_id, world.user_ids[0], changed_at=LATER
    )


def _noop_unconfirm(world):
    match_id = _match(world)
    return match_id, lambda: repo.unconfirm_match(match_id, changed_at=LATER)


def _noop_ready_claim(world, side: MatchSide):
    match_id = _match(world)
    _ready(match_id, side.name.lower(), world.user_ids[0])
    return match_id, lambda: repo.set_side_ready_flush(
        match_id, side, BASE, world.user_ids[0], changed_at=LATER
    )


def _noop_ready_revoke(world, side: MatchSide):
    match_id = _match(world)
    return match_id, lambda: repo.clear_side_ready_flush(
        match_id, side, changed_at=LATER
    )


def _noop_delete_absent_contestant(world):
    match_id = _match(world)
    _seat(match_id, participant_id=world.participant_ids[0])
    return match_id, lambda: repo.delete_contestant_from_match(
        match_id, participant_id=world.participant_ids[1], changed_at=LATER
    )


def _noop_delete_unknown_match_contestant(world):
    match_id = _match(world)
    _seat(match_id, participant_id=world.participant_ids[0])
    return match_id, lambda: repo.delete_match_contestant_flush(
        uuid7(), changed_at=LATER
    )


def _noop_delete_contestants_of_empty_match(world):
    match_id = _match(world)
    return match_id, lambda: repo.delete_contestants_for_match_flush(
        match_id, changed_at=LATER
    )


def _noop_remove_team_without_entries(world):
    match_id = _match(world)
    _seat(match_id, participant_id=world.participant_ids[0])
    return match_id, lambda: repo.remove_team_from_contestants_flush(
        world.team_id, changed_at=LATER
    )


# fmt: off
NOOP_WRITES = {
    'update_contestant_score': _noop_single_score,
    'update_contestant_scores': _noop_bulk_scores,
    'update_contestant_placement_and_points': _noop_placements,
    'clear_contestant_scores': _noop_clear_scores,
    'confirm_match': _noop_confirm,
    'unconfirm_match': _noop_unconfirm,
    'set_side_ready_flush_a': lambda w: _noop_ready_claim(w, MatchSide.A),
    'set_side_ready_flush_b': lambda w: _noop_ready_claim(w, MatchSide.B),
    'clear_side_ready_flush_a': lambda w: _noop_ready_revoke(w, MatchSide.A),
    'clear_side_ready_flush_b': lambda w: _noop_ready_revoke(w, MatchSide.B),
    'delete_contestant_from_match': _noop_delete_absent_contestant,
    'delete_match_contestant_flush': _noop_delete_unknown_match_contestant,
    'delete_contestants_for_match_flush': _noop_delete_contestants_of_empty_match,
    'remove_team_from_contestants_flush': _noop_remove_team_without_entries,
}
# fmt: on


@pytest.mark.parametrize('name', list(NOOP_WRITES))
def test_identical_scores_and_placements_do_not_touch(world, name):
    target, control, act = _arrange(world, NOOP_WRITES[name])
    before = _fingerprint(target)

    act()
    repo.commit_session()

    assert _last_changed(target) == BASE
    assert _fingerprint(target) == before
    assert _last_changed(control) == BASE


def test_a_bulk_write_touches_only_the_matches_that_changed(world):
    changed_match = _match(world)
    same_match = _match(world)
    changed = _seat(
        changed_match, participant_id=world.participant_ids[0], score=1
    )
    same = _seat(same_match, participant_id=world.participant_ids[1], score=1)
    placed_changed = _seat(
        changed_match,
        participant_id=world.participant_ids[1],
        placement=1,
        points=1,
    )
    placed_same = _seat(
        same_match,
        participant_id=world.participant_ids[0],
        placement=1,
        points=1,
    )
    db.session.commit()

    repo.update_contestant_scores({changed: 2, same: 1}, changed_at=LATER)
    repo.update_contestant_placement_and_points(
        {placed_changed: (1, 1), placed_same: (1, 1)},
        changed_at=LATER + timedelta(minutes=1),
    )
    repo.commit_session()

    assert _last_changed(changed_match) == LATER
    assert _last_changed(same_match) == BASE

    repo.update_contestant_placement_and_points(
        {placed_changed: (1, 1), placed_same: (2, 1)},
        changed_at=LATER + timedelta(minutes=2),
    )
    repo.commit_session()

    assert _last_changed(changed_match) == LATER
    assert _last_changed(same_match) == LATER + timedelta(minutes=2)


# -------------------------------------------------------------------- #
# the selected boundary: nothing else stamps
# -------------------------------------------------------------------- #


def _comment(match_id, world) -> TournamentMatchComment:
    return TournamentMatchComment(
        id=TournamentMatchCommentID(uuid7()),
        tournament_match_id=match_id,
        created_by=world.user_ids[0],
        comment='Ping the captains.',
        created_at=BASE,
    )


def _boundary_create_comment(world):
    match_id = _match(world)
    comment = _comment(match_id, world)
    return match_id, lambda: repo.create_match_comment(comment)


def _boundary_update_comment(world):
    match_id = _match(world)
    comment = _comment(match_id, world)
    repo.create_match_comment(comment)
    return match_id, lambda: repo.update_match_comment(comment.id, 'Edited.')


def _boundary_delete_comment(world):
    match_id = _match(world)
    comment = _comment(match_id, world)
    repo.create_match_comment(comment)
    return match_id, lambda: repo.delete_match_comment(comment.id)


def _boundary_delete_comments(world):
    match_id = _match(world)
    repo.create_match_comment(_comment(match_id, world))
    return match_id, lambda: repo.delete_comments_for_match_flush(match_id)


def _boundary_pin_and_unpin(world):
    match_id = _match(world)

    def act():
        pinned = repo.save_match_pin_flush(
            match_id,
            world.tournament_id,
            pinned_at=LATER,
            pinned_by=world.user_ids[0],
            updated_at=LATER,
            updated_by=world.user_ids[0],
            expected_revision=0,
        )
        assert pinned is not None
        unpinned = repo.save_match_pin_flush(
            match_id,
            world.tournament_id,
            pinned_at=None,
            pinned_by=None,
            updated_at=LATER,
            updated_by=world.user_ids[0],
            expected_revision=pinned.revision,
        )
        assert unpinned is not None

    return match_id, act


def _episode(match_id, world) -> MatchDueEpisode:
    return MatchDueEpisode(
        id=MatchDueEpisodeID(uuid7()),
        tournament_id=world.tournament_id,
        match_id=match_id,
        pairing_key='participant:a|participant:b',
        opened_at=LATER,
        opened_clock_us=1_000,
    )


def _boundary_episode_open_and_close(world):
    match_id = _match(world)

    def act():
        repo.open_due_episode_flush(_episode(match_id, world))
        repo.close_due_episodes_flush(
            [match_id], occurred_at=LATER, clock_us=2_000
        )

    return match_id, act


def _boundary_acknowledgement(world):
    match_id = _match(world)
    episode = _episode(match_id, world)

    def act():
        repo.open_due_episode_flush(episode)
        repo.create_escalation_ack_flush(
            MatchEscalationAcknowledgement(
                id=MatchEscalationAcknowledgementID(uuid7()),
                episode_id=episode.id,
                tournament_id=world.tournament_id,
                match_id=match_id,
                revision=1,
                occurred_at=LATER,
                clock_us=2_000,
                actor_id=world.user_ids[0],
                comment='Checked.',
            )
        )

    return match_id, act


def _boundary_retire_dashboard_matches(world):
    match_id = _match(world)

    def act():
        repo.open_due_episode_flush(_episode(match_id, world))
        repo.retire_dashboard_matches_flush([match_id], occurred_at=LATER)

    return match_id, act


def _boundary_routing(world):
    match_id = _match(world)
    next_id = _match(world)

    def act():
        repo.set_next_match_id_flush(match_id, next_id)
        repo.clear_next_match_id(match_id)
        repo.clear_loser_next_match_id(match_id)

    return match_id, act


def _boundary_notification_markers(world):
    match_id = _match(world)

    def act():
        repo.set_both_ready_notified_flush(match_id, LATER)
        repo.set_both_ready_notified_flush(match_id, None)
        repo.mark_matches_both_ready_notified([match_id], LATER, commit=False)

    return match_id, act


def _boundary_holds_and_revision(world):
    match_id = _match(world)

    def act():
        repo.set_side_invitation_hold_flush(match_id, MatchSide.A, True)
        repo.set_readiness_revision_flush(match_id, 5)
        repo.clear_match_readiness_flush(match_id, increment_revision=True)

    return match_id, act


def _boundary_occupancy(world):
    match_id = _match(world)
    return match_id, lambda: repo.set_occupied_since_if_unset_flush(
        match_id, LATER
    )


def _boundary_plain_orm_update(world):
    match_id = _match(world)

    def act():
        row = db.session.get(DbTournamentMatch, match_id)
        row.match_order = 9
        row.round = 4
        db.session.flush()

    return match_id, act


# fmt: off
BOUNDARY_WRITES = {
    'create_match_comment': _boundary_create_comment,
    'update_match_comment': _boundary_update_comment,
    'delete_match_comment': _boundary_delete_comment,
    'delete_comments_for_match_flush': _boundary_delete_comments,
    'pin_and_unpin': _boundary_pin_and_unpin,
    'episode_open_and_close': _boundary_episode_open_and_close,
    'escalation_acknowledgement': _boundary_acknowledgement,
    'retire_dashboard_matches_flush': _boundary_retire_dashboard_matches,
    'routing': _boundary_routing,
    'notification_markers': _boundary_notification_markers,
    'holds_and_revision': _boundary_holds_and_revision,
    'occupancy_marker': _boundary_occupancy,
    'plain_orm_column_update': _boundary_plain_orm_update,
}
# fmt: on


@pytest.mark.parametrize('name', list(BOUNDARY_WRITES))
def test_comments_and_annotations_do_not_touch(world, name):
    target, control, act = _arrange(world, BOUNDARY_WRITES[name])

    act()
    repo.commit_session()

    assert _last_changed(target) == BASE
    assert _last_changed(control) == BASE


# -------------------------------------------------------------------- #
# time: common, monotonic, owned by the caller's transaction
# -------------------------------------------------------------------- #


def test_timestamp_is_common_monotonic_and_rollback_owned(world):
    confirmed = _match(world)
    scored = _match(world)
    readied = _match(world)
    seated = _match(world)
    contestant = _seat(scored, participant_id=world.participant_ids[0], score=0)
    db.session.commit()
    matches = [confirmed, scored, readied, seated]

    def write(operation_time):
        repo.confirm_match(
            confirmed, world.user_ids[0], changed_at=operation_time
        )
        repo.update_contestant_scores(
            {contestant: 9}, changed_at=operation_time
        )
        repo.set_side_ready_flush(
            readied,
            MatchSide.A,
            operation_time,
            world.user_ids[0],
            changed_at=operation_time,
        )
        repo.create_match_contestant(
            TournamentMatchToContestant(
                id=uuid7(),
                tournament_match_id=seated,
                team_id=None,
                participant_id=world.participant_ids[1],
                score=None,
                created_at=BASE,
            ),
            changed_at=operation_time,
        )

    # Owned by the caller: nothing is durable before its commit, and its
    # rollback takes the stamps with the facts.
    write(LATER)
    assert [_last_changed(m) for m in matches] == [BASE] * 4
    repo.rollback_session()
    assert [_last_changed(m) for m in matches] == [BASE] * 4
    assert _fingerprint(confirmed)[0][0][0] is None

    # Common: one operation time, four writers, one value.
    write(LATER)
    repo.commit_session()
    assert [_last_changed(m) for m in matches] == [LATER] * 4

    # Monotonic: an older operation time stores its fact, not its time.
    repo.unconfirm_match(confirmed, changed_at=EARLIER)
    repo.update_contestant_scores({contestant: 1}, changed_at=EARLIER)
    repo.commit_session()
    assert _fingerprint(confirmed)[0][0][0] is None
    assert [_last_changed(m) for m in (confirmed, scored)] == [LATER] * 2

    later = LATER + timedelta(microseconds=1)
    repo.confirm_match(confirmed, world.user_ids[1], changed_at=later)
    repo.commit_session()
    assert _last_changed(confirmed) == later


@pytest.mark.parametrize(
    'name', [name for name in ACTUAL_WRITES if name not in COMMITS_ITSELF]
)
def test_every_stamping_writer_rolls_back_with_its_owner(world, name):
    target, control, act = _arrange(world, ACTUAL_WRITES[name])
    before = _fingerprint(target)

    act()
    repo.rollback_session()

    assert _last_changed(target) == BASE
    assert _fingerprint(target) == before
    assert _last_changed(control) == BASE


def test_the_stamp_and_the_legacy_score_share_one_commit(world):
    target, control, act = _arrange(world, _case_single_score)
    before = _fingerprint(target)

    act()

    assert _last_changed(target) == LATER
    assert _fingerprint(target) != before


def test_a_missing_operation_time_is_sampled_once_by_the_server(world):
    matches = [_match(world) for _ in range(3)]
    for match_id in matches:
        _seat(match_id, team_id=world.team_id)
    db.session.commit()

    window_start = repo.get_operation_time()
    repo.remove_team_from_contestants_flush(world.team_id)
    window_end = repo.get_operation_time()
    repo.commit_session()

    stamps = {_last_changed(match_id) for match_id in matches}
    assert len(stamps) == 1
    [stamp] = stamps
    assert window_start <= stamp <= window_end
    assert stamp.tzinfo is None


def test_an_aware_operation_time_is_stored_as_naive_utc(world):
    match_id = _match(world)
    db.session.commit()
    aware = datetime.fromisoformat('2026-10-07T15:00:00+02:00')

    db.session.execute(text("SET LOCAL TIME ZONE 'Asia/Tokyo'"))
    repo.confirm_match(match_id, world.user_ids[0], changed_at=aware)
    repo.commit_session()

    assert _last_changed(match_id) == datetime(2026, 10, 7, 13, 0, 0)
