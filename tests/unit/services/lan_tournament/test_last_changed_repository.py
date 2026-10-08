import ast
from datetime import datetime, timedelta, timezone
import inspect
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql.dml import Update
from sqlalchemy.sql.selectable import Select

from byceps.services.lan_tournament import tournament_repository as repo
from byceps.services.lan_tournament.dbmodels.match import DbTournamentMatch
from byceps.services.lan_tournament.models.operational_timing import (
    MatchDueEpisode,
    MatchDueEpisodeID,
    MatchEscalationAcknowledgement,
    MatchEscalationAcknowledgementID,
)
from byceps.services.lan_tournament.models.tournament import TournamentID
from byceps.services.lan_tournament.models.tournament_match import (
    MatchSide,
    TournamentMatchID,
)
from byceps.services.lan_tournament.models.tournament_match_comment import (
    TournamentMatchComment,
    TournamentMatchCommentID,
)
from byceps.services.lan_tournament.models.tournament_match_to_contestant import (
    TournamentMatchToContestant,
    TournamentMatchToContestantID,
)
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipantID,
)
from byceps.services.lan_tournament.models.tournament_team import (
    TournamentTeamID,
)
from byceps.services.user.models import UserID

from tests.helpers import generate_uuid


BASE = datetime(2026, 10, 7, 12, 0, 0)
LATER = BASE + timedelta(hours=1)

MATCH_ID = TournamentMatchID(generate_uuid())
OTHER_MATCH_ID = TournamentMatchID(generate_uuid())
USER_ID = UserID(generate_uuid())
TOURNAMENT_ID = TournamentID(generate_uuid())
TEAM_ID = TournamentTeamID(generate_uuid())
COMMENT_ID = TournamentMatchCommentID(generate_uuid())


def _contestant_id() -> TournamentMatchToContestantID:
    return TournamentMatchToContestantID(generate_uuid())


def _participant_id() -> TournamentParticipantID:
    return TournamentParticipantID(generate_uuid())


@pytest.fixture
def rows():
    return {}


@pytest.fixture
def session(rows):
    mock = MagicMock()
    mock.get.side_effect = lambda model, key, **kwargs: rows.get(key)
    with patch.object(repo.db, 'session', mock):
        yield mock


def _sql(statement) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


def _params(statement) -> dict:
    return statement.compile(dialect=postgresql.dialect()).params


def _executed(session) -> list:
    return [call.args[0] for call in session.execute.call_args_list]


def _stamps(session) -> list:
    """Return the statements that write a match's last change."""
    return [
        statement
        for statement in _executed(session)
        if isinstance(statement, Update)
        and 'SET last_changed_at=' in _sql(statement)
    ]


def _clock_samples(session) -> int:
    return sum(
        1
        for statement in _executed(session)
        if isinstance(statement, Select)
        and 'clock_timestamp' in _sql(statement)
    )


def _contestant(match_id, **facts):
    values = dict(
        tournament_match_id=match_id, score=None, placement=None, points=None
    )
    return SimpleNamespace(**{**values, **facts})


def _match_row(**facts):
    values = dict(
        confirmed_by=None,
        ready_at_a=None,
        ready_by_a=None,
        ready_at_b=None,
        ready_by_b=None,
    )
    return SimpleNamespace(**{**values, **facts})


# -------------------------------------------------------------------- #
# actual writes
# -------------------------------------------------------------------- #


def _create_contestant(session, rows):
    contestant = TournamentMatchToContestant(
        id=_contestant_id(),
        tournament_match_id=MATCH_ID,
        team_id=None,
        participant_id=_participant_id(),
        score=None,
        created_at=BASE,
    )
    return (
        lambda: repo.create_match_contestant(contestant, changed_at=LATER),
        [MATCH_ID],
        None,
    )


def _confirm(session, rows):
    rows[MATCH_ID] = _match_row()
    return (
        lambda: repo.confirm_match(MATCH_ID, USER_ID, changed_at=LATER),
        [MATCH_ID],
        ('confirmed_by IS DISTINCT FROM',),
    )


def _unconfirm(session, rows):
    rows[MATCH_ID] = _match_row(confirmed_by=USER_ID)
    return (
        lambda: repo.unconfirm_match(
            MATCH_ID, reset_readiness=False, changed_at=LATER
        ),
        [MATCH_ID],
        ('confirmed_by IS NOT NULL',),
    )


def _ready_claim(side):
    column = side.name.lower()

    def build(session, rows):
        rows[MATCH_ID] = _match_row()
        return (
            lambda: repo.set_side_ready_flush(
                MATCH_ID, side, BASE, USER_ID, changed_at=LATER
            ),
            [MATCH_ID],
            (
                f'ready_at_{column} IS DISTINCT FROM',
                f'ready_by_{column} IS DISTINCT FROM',
            ),
        )

    return build


def _ready_revoke(side):
    column = side.name.lower()

    def build(session, rows):
        rows[MATCH_ID] = _match_row()
        return (
            lambda: repo.clear_side_ready_flush(
                MATCH_ID, side, changed_at=LATER
            ),
            [MATCH_ID],
            (
                f'ready_at_{column} IS NOT NULL',
                f'ready_by_{column} IS NOT NULL',
            ),
        )

    return build


def _single_score(session, rows):
    contestant_id = _contestant_id()
    rows[contestant_id] = _contestant(MATCH_ID, score=1)
    return (
        lambda: repo.update_contestant_score(
            contestant_id, 2, changed_at=LATER
        ),
        [MATCH_ID],
        None,
    )


def _bulk_scores(session, rows):
    changed, same = _contestant_id(), _contestant_id()
    rows[changed] = _contestant(MATCH_ID, score=1)
    rows[same] = _contestant(OTHER_MATCH_ID, score=1)
    return (
        lambda: repo.update_contestant_scores(
            {changed: 2, same: 1}, changed_at=LATER
        ),
        [MATCH_ID],
        None,
    )


def _clear_scores(session, rows):
    session.query.return_value.filter_by.return_value.all.return_value = [
        _contestant(MATCH_ID, score=3),
        _contestant(MATCH_ID),
    ]
    return (
        lambda: repo.clear_contestant_scores(MATCH_ID, changed_at=LATER),
        [MATCH_ID],
        None,
    )


def _placements(session, rows):
    changed, same = _contestant_id(), _contestant_id()
    rows[changed] = _contestant(MATCH_ID, placement=1, points=3)
    rows[same] = _contestant(OTHER_MATCH_ID, placement=2, points=1)
    return (
        lambda: repo.update_contestant_placement_and_points(
            {changed: (1, 2), same: (2, 1)}, changed_at=LATER
        ),
        [MATCH_ID],
        None,
    )


def _delete_contestant_from_match(session, rows):
    session.scalars.return_value.all.return_value = [generate_uuid()]
    patches = (
        patch.object(repo, '_lock_pairing_subjects'),
        patch.object(repo, '_retire_match_pairing_flush'),
    )
    return _within(
        patches,
        lambda: repo.delete_contestant_from_match(
            MATCH_ID, participant_id=_participant_id(), changed_at=LATER
        ),
        [MATCH_ID],
    )


def _delete_match_contestant(session, rows):
    session.scalars.return_value.all.return_value = [MATCH_ID]
    patches = (
        patch.object(repo, '_lock_pairing_subjects'),
        patch.object(repo, '_retire_match_pairing_flush'),
    )
    return _within(
        patches,
        lambda: repo.delete_match_contestant_flush(
            _contestant_id(), changed_at=LATER
        ),
        [MATCH_ID],
    )


def _delete_contestants_for_match(session, rows):
    session.execute.return_value.scalars.return_value = [generate_uuid()]
    patches = (patch.object(repo, '_retire_match_pairing_flush'),)
    return _within(
        patches,
        lambda: repo.delete_contestants_for_match_flush(
            MATCH_ID, changed_at=LATER
        ),
        [MATCH_ID],
    )


def _remove_team_from_contestants(session, rows):
    session.scalars.return_value = [MATCH_ID, OTHER_MATCH_ID]
    patches = (
        patch.object(
            repo,
            'get_team',
            return_value=SimpleNamespace(tournament_id=TOURNAMENT_ID),
        ),
        patch.object(repo, 'lock_tournament_for_update'),
        patch.object(repo, 'lock_matches_for_update'),
        patch.object(repo, '_retire_match_pairing_flush'),
    )
    return _within(
        patches,
        lambda: repo.remove_team_from_contestants_flush(
            TEAM_ID, changed_at=LATER
        ),
        sorted([MATCH_ID, OTHER_MATCH_ID], key=str),
    )


def _within(patches, act, expected_ids):
    def run():
        for item in patches:
            item.start()
        try:
            act()
        finally:
            for item in patches:
                item.stop()

    return run, expected_ids, None


# fmt: off
ACTUAL_WRITES = {
    'create_match_contestant': _create_contestant,
    'confirm_match': _confirm,
    'unconfirm_match': _unconfirm,
    'update_contestant_score': _single_score,
    'update_contestant_scores': _bulk_scores,
    'clear_contestant_scores': _clear_scores,
    'update_contestant_placement_and_points': _placements,
    'delete_contestant_from_match': _delete_contestant_from_match,
    'delete_match_contestant_flush': _delete_match_contestant,
    'delete_contestants_for_match_flush': _delete_contestants_for_match,
    'remove_team_from_contestants_flush': _remove_team_from_contestants,
    'set_side_ready_flush_a': _ready_claim(MatchSide.A),
    'set_side_ready_flush_b': _ready_claim(MatchSide.B),
    'clear_side_ready_flush_a': _ready_revoke(MatchSide.A),
    'clear_side_ready_flush_b': _ready_revoke(MatchSide.B),
}
# fmt: on


@pytest.mark.parametrize('name', list(ACTUAL_WRITES))
def test_actual_domain_writes_touch_last_changed(session, rows, name):
    act, expected_ids, predicate = ACTUAL_WRITES[name](session, rows)

    act()

    [stamp] = _stamps(session)
    assert _params(stamp)['id_1'] == expected_ids
    assert _params(stamp)['greatest_1'] == LATER
    assert _clock_samples(session) == 0
    for fragment in predicate or ():
        assert fragment in _sql(stamp)


# -------------------------------------------------------------------- #
# no-op writes
# -------------------------------------------------------------------- #


def _same_single_score(session, rows):
    contestant_id = _contestant_id()
    rows[contestant_id] = _contestant(MATCH_ID, score=5)
    return lambda: repo.update_contestant_score(contestant_id, 5)


def _same_bulk_scores(session, rows):
    first, second = _contestant_id(), _contestant_id()
    rows[first] = _contestant(MATCH_ID, score=5)
    rows[second] = _contestant(OTHER_MATCH_ID, score=0)
    return lambda: repo.update_contestant_scores({first: 5, second: 0})


def _same_placements(session, rows):
    first, second = _contestant_id(), _contestant_id()
    rows[first] = _contestant(MATCH_ID, placement=1, points=3)
    rows[second] = _contestant(OTHER_MATCH_ID, placement=2, points=0)
    return lambda: repo.update_contestant_placement_and_points(
        {first: (1, 3), second: (2, 0)}
    )


def _already_clear_scores(session, rows):
    session.query.return_value.filter_by.return_value.all.return_value = [
        _contestant(MATCH_ID),
        _contestant(MATCH_ID),
    ]
    return lambda: repo.clear_contestant_scores(MATCH_ID)


def _nothing_deleted(session, rows):
    session.scalars.return_value.all.return_value = []
    patches = (
        patch.object(repo, '_lock_pairing_subjects'),
        patch.object(repo, '_retire_match_pairing_flush'),
    )
    act, _, _ = _within(
        patches,
        lambda: repo.delete_contestant_from_match(
            MATCH_ID, participant_id=_participant_id()
        ),
        [],
    )
    return act


def _no_match_contestant(session, rows):
    session.scalars.return_value.all.return_value = []
    patches = (
        patch.object(repo, '_lock_pairing_subjects'),
        patch.object(repo, '_retire_match_pairing_flush'),
    )
    act, _, _ = _within(
        patches,
        lambda: repo.delete_match_contestant_flush(_contestant_id()),
        [],
    )
    return act


def _empty_match(session, rows):
    session.execute.return_value.scalars.return_value = []
    patches = (patch.object(repo, '_retire_match_pairing_flush'),)
    act, _, _ = _within(
        patches,
        lambda: repo.delete_contestants_for_match_flush(MATCH_ID),
        [],
    )
    return act


def _team_in_no_match(session, rows):
    session.scalars.return_value = []
    patches = (
        patch.object(
            repo,
            'get_team',
            return_value=SimpleNamespace(tournament_id=TOURNAMENT_ID),
        ),
        patch.object(repo, 'lock_tournament_for_update'),
        patch.object(repo, 'lock_matches_for_update'),
        patch.object(repo, '_retire_match_pairing_flush'),
    )
    act, _, _ = _within(
        patches,
        lambda: repo.remove_team_from_contestants_flush(TEAM_ID),
        [],
    )
    return act


# fmt: off
NOOP_WRITES = {
    'update_contestant_score': _same_single_score,
    'update_contestant_scores': _same_bulk_scores,
    'update_contestant_placement_and_points': _same_placements,
    'clear_contestant_scores': _already_clear_scores,
    'delete_contestant_from_match': _nothing_deleted,
    'delete_match_contestant_flush': _no_match_contestant,
    'delete_contestants_for_match_flush': _empty_match,
    'remove_team_from_contestants_flush': _team_in_no_match,
}
# fmt: on


@pytest.mark.parametrize('name', list(NOOP_WRITES))
def test_identical_scores_and_placements_do_not_touch(session, rows, name):
    act = NOOP_WRITES[name](session, rows)

    act()

    assert _stamps(session) == []
    assert _clock_samples(session) == 0


# -------------------------------------------------------------------- #
# the selected boundary
# -------------------------------------------------------------------- #


def _episode() -> MatchDueEpisode:
    return MatchDueEpisode(
        id=MatchDueEpisodeID(generate_uuid()),
        tournament_id=TOURNAMENT_ID,
        match_id=MATCH_ID,
        pairing_key='participant:a|participant:b',
        opened_at=BASE,
        opened_clock_us=1_000,
    )


def _acknowledgement() -> MatchEscalationAcknowledgement:
    return MatchEscalationAcknowledgement(
        id=MatchEscalationAcknowledgementID(generate_uuid()),
        episode_id=MatchDueEpisodeID(generate_uuid()),
        tournament_id=TOURNAMENT_ID,
        match_id=MATCH_ID,
        revision=1,
        occurred_at=BASE,
        clock_us=2_000,
        actor_id=USER_ID,
    )


def _comment() -> TournamentMatchComment:
    return TournamentMatchComment(
        id=TournamentMatchCommentID(generate_uuid()),
        tournament_match_id=MATCH_ID,
        created_by=USER_ID,
        comment='Ping the captains.',
        created_at=BASE,
    )


def _plain_row(**facts):
    values = dict(
        comment='',
        next_match_id=None,
        loser_next_match_id=None,
        both_ready_notified_at=None,
        occupied_since=None,
        ready_at_a=None,
        ready_at_b=None,
        ready_by_a=None,
        ready_by_b=None,
        invitation_hold_a=False,
        invitation_hold_b=False,
        readiness_revision=0,
        pairing_generation=0,
    )
    return SimpleNamespace(**{**values, **facts})


# fmt: off
BOUNDARY_WRITES = {
    'create_match_comment': lambda: repo.create_match_comment(_comment()),
    'update_match_comment': lambda: repo.update_match_comment(COMMENT_ID, 'Edited.'),
    'delete_match_comment': lambda: repo.delete_match_comment(COMMENT_ID),
    'delete_comments_for_match': lambda: repo.delete_comments_for_match(MATCH_ID),
    'delete_comments_for_match_flush': lambda: repo.delete_comments_for_match_flush(MATCH_ID),
    'open_due_episode_flush': lambda: repo.open_due_episode_flush(_episode()),
    'close_due_episodes_flush': lambda: repo.close_due_episodes_flush([MATCH_ID], occurred_at=LATER, clock_us=2_000),
    'retire_dashboard_matches_flush': lambda: repo.retire_dashboard_matches_flush([MATCH_ID], occurred_at=LATER),
    'create_escalation_ack_flush': lambda: repo.create_escalation_ack_flush(_acknowledgement()),
    'save_match_pin_flush': lambda: repo.save_match_pin_flush(MATCH_ID, TOURNAMENT_ID, pinned_at=LATER, pinned_by=USER_ID, updated_at=LATER, updated_by=USER_ID, expected_revision=0),
    'set_next_match_id_flush': lambda: repo.set_next_match_id_flush(MATCH_ID, OTHER_MATCH_ID),
    'clear_next_match_id': lambda: repo.clear_next_match_id(MATCH_ID),
    'clear_loser_next_match_id': lambda: repo.clear_loser_next_match_id(MATCH_ID),
    'set_both_ready_notified_flush': lambda: repo.set_both_ready_notified_flush(MATCH_ID, LATER),
    'mark_matches_both_ready_notified': lambda: repo.mark_matches_both_ready_notified([MATCH_ID], LATER, commit=False),
    'set_side_invitation_hold_flush': lambda: repo.set_side_invitation_hold_flush(MATCH_ID, MatchSide.A, True),
    'set_readiness_revision_flush': lambda: repo.set_readiness_revision_flush(MATCH_ID, 5),
    'clear_match_readiness_flush': lambda: repo.clear_match_readiness_flush(MATCH_ID, increment_revision=True),
    'set_occupied_since_if_unset_flush': lambda: repo.set_occupied_since_if_unset_flush(MATCH_ID, LATER),
}
# fmt: on


@pytest.mark.parametrize('name', list(BOUNDARY_WRITES))
def test_comments_and_annotations_do_not_touch(session, rows, name):
    rows[MATCH_ID] = rows[COMMENT_ID] = _plain_row()

    BOUNDARY_WRITES[name]()

    assert _stamps(session) == []
    assert _clock_samples(session) == 0


def _calls_of(function_node) -> set[str]:
    names = set()
    for node in ast.walk(function_node):
        if isinstance(node, ast.Call):
            names.add(ast.unparse(node.func))
    return names


def _repository_functions() -> dict[str, ast.FunctionDef]:
    tree = ast.parse(inspect.getsource(repo))
    return {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
    }


# fmt: off
STAMPING_WRITERS = {
    'create_match_contestant', 'confirm_match', 'unconfirm_match',
    'update_contestant_score', 'update_contestant_scores',
    'clear_contestant_scores', 'update_contestant_placement_and_points',
    'delete_match_contestant_flush', 'delete_contestant_from_match',
    'delete_contestants_for_match_flush', 'remove_team_from_contestants_flush',
    'set_side_ready_flush', 'clear_side_ready_flush',
}
TOUCH_HELPERS = {
    '_touch_matches', 'touch_matches_last_changed_flush',
    '_touch_changed_matches_flush', '_touch_match_if',
}
# fmt: on


def test_only_the_selected_writers_reach_the_touch_helpers():
    functions = _repository_functions()

    callers = {
        name
        for name, node in functions.items()
        if _calls_of(node) & TOUCH_HELPERS
    }

    assert callers == STAMPING_WRITERS | (TOUCH_HELPERS - {'_touch_matches'})


def test_only_creation_and_the_touch_helpers_write_the_last_change():
    functions = _repository_functions()

    writers = {
        name
        for name, node in functions.items()
        if 'last_changed_at' in ast.unparse(node)
    }

    assert writers == {
        '_db_match_to_match',
        '_initial_last_changed_at',
        '_touch_matches',
        'create_match',
    }


def test_the_column_has_no_orm_default_or_onupdate():
    column = DbTournamentMatch.__table__.c.last_changed_at

    assert column.default is None
    assert column.onupdate is None
    assert column.server_default is None
    assert column.server_onupdate is None


# -------------------------------------------------------------------- #
# time
# -------------------------------------------------------------------- #


def test_timestamp_is_common_monotonic_and_rollback_owned(session, rows):
    rows[MATCH_ID] = _match_row()
    contestant_id = _contestant_id()
    rows[contestant_id] = _contestant(OTHER_MATCH_ID, score=0)

    repo.confirm_match(MATCH_ID, USER_ID, changed_at=LATER)
    repo.update_contestant_scores({contestant_id: 9}, changed_at=LATER)
    repo.set_side_ready_flush(
        MATCH_ID, MatchSide.A, LATER, USER_ID, changed_at=LATER
    )

    stamps = _stamps(session)
    # Common: one value for every writer, and the server clock never asked.
    assert len(stamps) == 3
    assert {_params(stamp)['greatest_1'] for stamp in stamps} == {LATER}
    assert _clock_samples(session) == 0
    # Monotonic: the stored time can only stay or move forward.
    for stamp in stamps:
        assert (
            'last_changed_at=greatest(lan_tournament_matches.last_changed_at,'
            in _sql(stamp)
        )
    # Owned by the caller: flush-only, no commit and no rollback inside.
    session.commit.assert_not_called()
    session.rollback.assert_not_called()


# fmt: off
@pytest.mark.parametrize(
    'name', [name for name in ACTUAL_WRITES if name != 'update_contestant_score']
)
# fmt: on
def test_every_stamping_writer_leaves_commit_and_rollback_to_the_owner(
    session, rows, name
):
    act, _, _ = ACTUAL_WRITES[name](session, rows)

    act()

    session.commit.assert_not_called()
    session.rollback.assert_not_called()


def test_the_legacy_score_writer_stamps_before_its_own_commit(session, rows):
    act, _, _ = _single_score(session, rows)

    act()

    names = [call[0] for call in session.method_calls]
    assert names.count('commit') == 1
    assert names.index('execute') < names.index('commit')
    assert len(_stamps(session)) == 1


def test_a_missing_operation_time_is_sampled_once_per_call(session, rows):
    session.scalars.return_value = [MATCH_ID, OTHER_MATCH_ID, generate_uuid()]
    sampled = datetime(2026, 10, 7, 13, 30, 0)
    session.execute.return_value.scalar_one.return_value = sampled
    patches = (
        patch.object(
            repo, 'get_team', return_value=SimpleNamespace(tournament_id=TOURNAMENT_ID)
        ),
        patch.object(repo, 'lock_tournament_for_update'),
        patch.object(repo, 'lock_matches_for_update'),
        patch.object(repo, '_retire_match_pairing_flush'),
    )

    for item in patches:
        item.start()
    try:
        repo.remove_team_from_contestants_flush(TEAM_ID)
    finally:
        for item in patches:
            item.stop()

    [stamp] = _stamps(session)
    assert _clock_samples(session) == 1
    assert len(_params(stamp)['id_1']) == 3
    assert _params(stamp)['greatest_1'] == sampled


# fmt: off
@pytest.mark.parametrize(
    'given, stored',
    [
        (datetime(2026, 10, 7, 12), datetime(2026, 10, 7, 12)),
        (datetime(2026, 10, 7, 14, tzinfo=timezone(timedelta(hours=2))), datetime(2026, 10, 7, 12)),
        (datetime(2026, 10, 7, 5, tzinfo=timezone(timedelta(hours=-7))), datetime(2026, 10, 7, 12)),
    ],
    ids=['naive-is-utc', 'plus-two', 'minus-seven'],
)
# fmt: on
def test_the_stamp_is_naive_utc(session, rows, given, stored):
    rows[MATCH_ID] = _match_row()

    repo.confirm_match(MATCH_ID, USER_ID, changed_at=given)

    [stamp] = _stamps(session)
    assert _params(stamp)['greatest_1'] == stored
    assert _params(stamp)['greatest_1'].tzinfo is None
