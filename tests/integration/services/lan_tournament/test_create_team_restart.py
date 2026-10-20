"""
tests.integration.services.lan_tournament.test_create_team_restart
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Create a team in a running team tournament by restarting it.
"""

import dataclasses
from datetime import datetime, UTC
from itertools import count

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from byceps.database import db
from byceps.services.lan_tournament import (
    signals,
    tournament_log_service,
    tournament_match_service,
    tournament_qualification_repository,
    tournament_repository,
    tournament_restart_service as restart_service,
    tournament_seeding_repository,
    tournament_seeding_service as seeding,
    tournament_service,
    tournament_team_service as team_service,
)
from byceps.services.lan_tournament.dbmodels.tournament import DbTournament
from byceps.services.lan_tournament.models import (
    ContestantType,
    EliminationMode,
    GameFormat,
    TournamentStatus,
)
from byceps.services.lan_tournament.models.qualification_decision import (
    DecisionBlock,
    QualificationDecision,
    QualificationDecisionID,
)
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipant,
    TournamentParticipantID,
)
from byceps.services.lan_tournament.models.tournament_seeding import (
    TournamentSeeding,
    TournamentSeedingID,
)
from byceps.services.lan_tournament.models.tournament_team import (
    TournamentTeam,
    TournamentTeamID,
)
from byceps.services.party.models import PartyID
from byceps.util.uuid import generate_uuid7


ONGOING = TournamentStatus.ONGOING
PAUSED = TournamentStatus.PAUSED
CLOSED = TournamentStatus.REGISTRATION_CLOSED

REASON = 'A late team joins'

_counter = count(1)


@pytest.fixture(scope='module')
def party(make_party, brand):
    return make_party(
        brand,
        PartyID(f'create-team-restart-{generate_uuid7()}'),
        'Create Team Restart',
    )


@pytest.fixture(scope='module')
def players(make_user):
    suffix = str(generate_uuid7())[:12]
    return [make_user(f'CreateTeamRestart{suffix}P{i}') for i in range(8)]


@pytest.fixture(scope='module')
def admin(make_user):
    return make_user(f'CreateTeamRestartAdmin{str(generate_uuid7())[:12]}')


@pytest.fixture(autouse=True)
def _session(party):
    """Provide the app context, and leave no open transaction behind."""
    yield
    db.session.rollback()


@pytest.fixture
def build(party, players, admin):
    """Build a team tournament that is generated and then started.

    The first four players each captain a team. The others are not
    registered.
    """

    def _build(
        status=PAUSED,
        *,
        team_count=4,
        contestant_type=ContestantType.TEAM,
        start=True,
        free_players=0,
        phase_two_state=False,
        **tournament_args,
    ):
        result = tournament_service.create_tournament(
            party.id,
            f'Create team restart {next(_counter)} {generate_uuid7()}',
            contestant_type=contestant_type,
            game_format=GameFormat.ONE_V_ONE,
            elimination_mode=EliminationMode.SINGLE_ELIMINATION,
            tournament_status=CLOSED,
            **tournament_args,
        )
        assert result.is_ok(), result.unwrap_err()
        tournament, _ = result.unwrap()

        now = datetime.now(UTC).replace(tzinfo=None)
        for index, player in enumerate(players[: team_count + free_players]):
            team_id = None
            if contestant_type is ContestantType.TEAM and index < team_count:
                team_id = TournamentTeamID(generate_uuid7())
                tournament_repository.create_team(
                    TournamentTeam(
                        id=team_id,
                        tournament_id=tournament.id,
                        name=f'Existing team {generate_uuid7()}',
                        tag=None,
                        description=None,
                        image_url=None,
                        captain_user_id=player.id,
                        join_code=None,
                        created_at=now,
                    )
                )
                db.session.flush()
            tournament_repository.create_participant(
                TournamentParticipant(
                    id=TournamentParticipantID(generate_uuid7()),
                    user_id=player.id,
                    tournament_id=tournament.id,
                    substitute_player=False,
                    team_id=team_id,
                    created_at=now,
                )
            )
        db.session.commit()

        board = seeding.get_board(tournament.id, initiator_id=admin.id)
        generated = seeding.generate_from_seeding(
            tournament.id,
            expected_version=board.unwrap().version,
            initiator_id=admin.id,
        )
        assert generated.is_ok(), generated.unwrap_err()

        if start:
            _change_status(tournament, ONGOING, admin)
            if status is PAUSED:
                _change_status(tournament, PAUSED, admin)
        if phase_two_state:
            _add_phase_two_state(tournament, admin)
        return tournament

    return _build


def _change_status(tournament, status, admin):
    changed = tournament_service.change_status(tournament.id, status, admin.id)
    assert changed.is_ok(), changed.unwrap_err()


def _add_phase_two_state(tournament, admin):
    """Give the tournament what a restart would wipe besides its matches."""
    now = datetime.now(UTC).replace(tzinfo=None)
    db_tournament = db.session.get(DbTournament, tournament.id)
    db_tournament.playoff_released_at = now
    db_tournament.playoff_released_by = admin.id
    db_tournament.playoff_auto_release_suspended = True
    tournament_qualification_repository.upsert_decision(
        QualificationDecision(
            id=QualificationDecisionID(generate_uuid7()),
            tournament_id=tournament.id,
            scope='group:0',
            blocks=(
                DecisionBlock(
                    contestant_ids=('a', 'b'),
                    reason='Tie',
                    decided_by=admin.id,
                    decided_at=now,
                ),
            ),
            reason='Tie',
            decided_by=admin.id,
            decided_at=now,
        )
    )
    tournament_seeding_repository.create_seeding(
        TournamentSeeding(
            id=TournamentSeedingID(generate_uuid7()),
            tournament_id=tournament.id,
            target='playoff',
            seed_code='a b',
            version=1,
            generated_seed_code=None,
            generated_at=None,
            updated_by=admin.id,
            created_at=now,
            updated_at=now,
        )
    )
    db.session.commit()


def _create(tournament, admin, captain, **overrides):
    args = {
        'reason': REASON,
        'confirm_restart': True,
        'initiator_id': admin.id,
    }
    args.update(overrides)
    name = args.pop('name', f'Late team {generate_uuid7()}')
    return restart_service.create_team_with_restart(
        tournament.id, name, captain.id, **args
    )


def _state(tournament):
    """Read everything a restart touches, from the database."""
    db.session.rollback()
    db.session.expire_all()
    return {
        'tournament': tournament_repository.get_tournament(
            tournament.id, fresh=True
        ),
        'matches': tournament_match_service.get_matches_for_tournament_ordered(
            tournament.id
        ),
        'contestants': tournament_repository.get_contestants_for_tournament(
            tournament.id
        ),
        'seedings': tournament_seeding_repository.get_seedings_for_tournament(
            tournament.id
        ),
        'decisions': (
            tournament_qualification_repository.get_decisions_for_tournament(
                tournament.id
            )
        ),
        'teams': tournament_repository.get_teams_for_tournament(
            tournament.id, include_removed=True
        ),
        'participants': tournament_repository.get_participants_for_tournament(
            tournament.id, include_removed=True
        ),
        'log': [
            e.event_type
            for e in tournament_log_service.get_entries_for_tournament(
                tournament.id
            )
        ],
    }


def _entries(tournament, event_type):
    db.session.rollback()
    return [
        e
        for e in tournament_log_service.get_entries_for_tournament(
            tournament.id
        )
        if e.event_type == event_type
    ]


def _committed(tournament_id):
    """Read the committed state through a connection of its own."""
    params = {'t': str(tournament_id)}
    with db.engine.connect() as connection:
        status = connection.execute(
            text('select tournament_status from lan_tournaments where id = :t'),
            params,
        ).scalar()
        matches = connection.execute(
            text(
                'select count(*) from lan_tournament_matches'
                ' where tournament_id = :t'
            ),
            params,
        ).scalar()
        teams = connection.execute(
            text(
                'select count(*) from lan_tournament_teams'
                ' where tournament_id = :t and removed_at is null'
            ),
            params,
        ).scalar()
    return status, matches, teams


def _assert_unlocked(tournament):
    """Prove the service ended its transaction, so it holds no row lock."""
    with db.engine.begin() as connection:
        connection.execute(
            text(
                'select id from lan_tournaments where id = :t for update nowait'
            ),
            {'t': str(tournament.id)},
        )


class _Probe:
    """Collect the events of the four signals, with the committed state."""

    def __init__(self, tournament_id):
        self.tournament_id = tournament_id
        self.seen = []
        self._connected = [
            (name, signal, self._receiver(name))
            for name, signal in (
                ('match_deleted', signals.match_deleted),
                ('status_changed', signals.tournament_status_changed),
                ('participant_joined', signals.participant_joined),
                ('team_created', signals.team_created),
            )
        ]

    def _receiver(self, name):
        def receive(sender, *, event):
            if getattr(event, 'tournament_id', None) == self.tournament_id:
                self.seen.append((name, event, _committed(self.tournament_id)))

        return receive

    def __enter__(self):
        for _, signal, receiver in self._connected:
            signal.connect(receiver)
        return self

    def __exit__(self, *exc_info):
        for _, signal, receiver in self._connected:
            signal.disconnect(receiver)

    @property
    def names(self):
        return [name for name, _, _ in self.seen]


@pytest.fixture
def flush_calls(monkeypatch):
    """Count the calls of the restart flush, the first destructive write."""
    calls = []
    real = restart_service.restart_tournament_flush

    def spy(*args, **kwargs):
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(restart_service, 'restart_tournament_flush', spy)
    return calls


def _assert_refused_and_kept(
    tournament, before, probe, flush_calls, result, error, *, flushed=False
):
    """Check the refusal, the untouched state and the silent signals."""
    assert result.is_err()
    assert result.unwrap_err() == error
    _assert_unlocked(tournament)
    assert (len(flush_calls) > 0) is flushed
    assert probe.seen == []
    assert _state(tournament) == before


@pytest.mark.parametrize('status', [ONGOING, PAUSED])
def test_create_team_with_restart_registers_the_captain_and_creates_the_team(
    build, players, admin, status
):
    tournament = build(status)
    captain = players[5]
    old_matches = _state(tournament)['matches']
    assert old_matches
    old_teams = len(_state(tournament)['teams'])
    assert (
        tournament_repository.find_participant_by_user(
            tournament.id, captain.id
        )
        is None
    )

    with _Probe(tournament.id) as probe:
        result = _create(
            tournament, admin, captain, name='Latecomers', tag='lc'
        )

    assert result.is_ok(), result.unwrap_err()
    team, event = result.unwrap()
    assert team.name == 'Latecomers'
    assert team.tag == 'LC'
    assert team.captain_user_id == captain.id
    assert event.team_id == team.id
    assert event.tournament_id == tournament.id

    state = _state(tournament)
    assert state['tournament'].tournament_status is CLOSED
    assert state['matches'] == []
    assert len(state['teams']) == old_teams + 1
    (registered,) = [
        p for p in state['participants'] if p.user_id == captain.id
    ]
    assert registered.team_id == team.id
    assert registered.substitute_player is False
    assert registered.removed_at is None

    (entry,) = _entries(tournament, 'tournament-restarted')
    assert entry.initiator_id == admin.id
    assert entry.data['reason'] == REASON
    assert entry.data['old_status'] == status.name
    assert entry.data['team_name'] == 'Latecomers'
    assert entry.data['captain_user_id'] == str(captain.id)
    assert 'bracket-cleared' in state['log']

    assert probe.names == (
        ['match_deleted'] * len(old_matches)
        + ['status_changed', 'participant_joined', 'team_created']
    )
    for _, _, committed in probe.seen:
        assert committed == (
            CLOSED.name,
            0,
            old_teams + 1,
        )
    joined = [e for n, e, _ in probe.seen if n == 'participant_joined']
    assert joined[0].participant_id == registered.id
    created = [e for n, e, _ in probe.seen if n == 'team_created']
    assert created == [event]


def test_create_team_with_restart_uses_a_registered_free_captain(
    build, players, admin
):
    tournament = build(PAUSED, free_players=1, max_players=5)
    captain = players[4]
    before = _state(tournament)
    (free,) = [p for p in before['participants'] if p.user_id == captain.id]
    assert free.team_id is None
    assert len(before['participants']) == 5

    with _Probe(tournament.id) as probe:
        result = _create(tournament, admin, captain)

    assert result.is_ok(), result.unwrap_err()
    team, _ = result.unwrap()
    state = _state(tournament)
    assert state['tournament'].tournament_status is CLOSED
    assert len(state['participants']) == 5
    (seated,) = [p for p in state['participants'] if p.user_id == captain.id]
    assert seated.id == free.id
    assert seated.team_id == team.id
    assert 'participant_joined' not in probe.names
    assert probe.names[-2:] == ['status_changed', 'team_created']


def test_restarted_tournament_regenerates_and_starts_with_the_new_team(
    build, players, admin
):
    tournament = build(PAUSED)
    captain = players[5]

    result = _create(tournament, admin, captain)
    assert result.is_ok(), result.unwrap_err()
    team, _ = result.unwrap()
    assert _state(tournament)['tournament'].tournament_status is CLOSED

    board = seeding.get_board(tournament.id, initiator_id=admin.id).unwrap()
    reseeded = seeding.apply_action(
        tournament.id,
        seeding.INITIAL_TARGET,
        seeding.ReseedKeepTiers(),
        expected_version=board.version,
        initiator_id=admin.id,
    )
    assert reseeded.is_ok(), reseeded.unwrap_err()

    board = seeding.get_board(tournament.id, initiator_id=admin.id).unwrap()
    generated = seeding.generate_from_seeding(
        tournament.id, expected_version=board.version, initiator_id=admin.id
    )
    assert generated.is_ok(), generated.unwrap_err()

    started = tournament_service.change_status(
        tournament.id, ONGOING, admin.id, confirm_generated_layout=True
    )
    assert started.is_ok(), started.unwrap_err()

    state = _state(tournament)
    assert state['tournament'].tournament_status is ONGOING
    assert state['matches']
    team_ids = {
        c.team_id
        for members in state['contestants'].values()
        for c in members
        if c.team_id is not None
    }
    assert team.id in team_ids
    assert len(team_ids) == 5


def test_create_team_with_restart_refuses_without_confirmation_and_keeps_the_bracket(
    build, players, admin, flush_calls
):
    tournament = build(PAUSED, phase_two_state=True)
    before = _state(tournament)

    with _Probe(tournament.id) as probe:
        result = _create(tournament, admin, players[5], confirm_restart=False)

    _assert_refused_and_kept(
        tournament,
        before,
        probe,
        flush_calls,
        result,
        restart_service.RESTART_NOT_CONFIRMED_ERROR,
    )


@pytest.mark.parametrize('reason', ['', '   ', '\n\t'])
def test_create_team_with_restart_refuses_a_blank_reason_and_keeps_the_bracket(
    build, players, admin, flush_calls, reason
):
    tournament = build(PAUSED, phase_two_state=True)
    before = _state(tournament)

    with _Probe(tournament.id) as probe:
        result = _create(tournament, admin, players[5], reason=reason)

    _assert_refused_and_kept(
        tournament,
        before,
        probe,
        flush_calls,
        result,
        restart_service.RESTART_REASON_MISSING_ERROR,
    )


def test_create_team_with_restart_refuses_a_duplicate_name_and_keeps_the_bracket(
    build, players, admin, flush_calls
):
    tournament = build(ONGOING, phase_two_state=True)
    before = _state(tournament)
    existing = before['teams'][0].name

    with _Probe(tournament.id) as probe:
        result = _create(tournament, admin, players[5], name=existing.upper())

    _assert_refused_and_kept(
        tournament,
        before,
        probe,
        flush_calls,
        result,
        'A team with this name already exists in this tournament.',
    )
    assert (
        tournament_repository.find_participant_by_user(
            tournament.id, players[5].id
        )
        is None
    )


def test_create_team_with_restart_refuses_a_duplicate_tag_and_keeps_the_bracket(
    build, players, admin, flush_calls
):
    tournament = build(PAUSED, phase_two_state=True)
    (first, *_) = tournament_repository.get_teams_for_tournament(tournament.id)
    tournament_repository.update_team(dataclasses.replace(first, tag='ABC'))
    db.session.commit()
    before = _state(tournament)

    with _Probe(tournament.id) as probe:
        result = _create(tournament, admin, players[5], tag='abc')

    _assert_refused_and_kept(
        tournament,
        before,
        probe,
        flush_calls,
        result,
        'A team with this tag already exists in this tournament.',
    )


def test_create_team_with_restart_refuses_when_teams_are_full_and_keeps_the_bracket(
    build, players, admin, flush_calls
):
    tournament = build(PAUSED, max_teams=4, phase_two_state=True)
    before = _state(tournament)
    assert len(before['teams']) == 4

    with _Probe(tournament.id) as probe:
        result = _create(tournament, admin, players[5])

    _assert_refused_and_kept(
        tournament,
        before,
        probe,
        flush_calls,
        result,
        'Maximum number of teams reached.',
    )


def test_create_team_with_restart_refuses_a_captain_already_on_a_team_and_keeps_the_bracket(
    build, players, admin, flush_calls
):
    tournament = build(ONGOING, phase_two_state=True)
    before = _state(tournament)

    with _Probe(tournament.id) as probe:
        result = _create(tournament, admin, players[0])

    _assert_refused_and_kept(
        tournament,
        before,
        probe,
        flush_calls,
        result,
        'The team captain is already assigned to a team.',
    )


def test_create_team_with_restart_refuses_a_new_captain_when_the_tournament_is_full_and_keeps_the_bracket(
    build, players, admin, flush_calls
):
    tournament = build(PAUSED, max_players=4, phase_two_state=True)
    before = _state(tournament)
    assert len(before['participants']) == 4

    with _Probe(tournament.id) as probe:
        result = _create(tournament, admin, players[5])

    _assert_refused_and_kept(
        tournament,
        before,
        probe,
        flush_calls,
        result,
        'Tournament is full.',
    )


def test_create_team_with_restart_refuses_an_invalid_image_url_and_keeps_the_bracket(
    build, players, admin, flush_calls
):
    tournament = build(PAUSED, phase_two_state=True)
    before = _state(tournament)

    with _Probe(tournament.id) as probe:
        result = _create(
            tournament, admin, players[5], image_url='javascript:alert(1)'
        )

    _assert_refused_and_kept(
        tournament,
        before,
        probe,
        flush_calls,
        result,
        'Image URL must use http or https scheme.',
    )


@pytest.mark.parametrize(
    ('options', 'error'),
    [
        (
            {'start': False},
            restart_service.RESTART_NOT_RUNNING_ERROR,
        ),
        (
            {'contestant_type': ContestantType.SOLO},
            restart_service.RESTART_NOT_TEAM_ERROR,
        ),
    ],
)
def test_create_team_with_restart_refuses_a_tournament_that_needs_no_restart_and_keeps_the_bracket(
    build, players, admin, flush_calls, options, error
):
    tournament = build(PAUSED, phase_two_state=True, **options)
    before = _state(tournament)
    assert before['matches']

    with _Probe(tournament.id) as probe:
        result = _create(tournament, admin, players[5])

    _assert_refused_and_kept(
        tournament, before, probe, flush_calls, result, error
    )


def test_create_team_with_restart_refuses_a_raced_duplicate_name_and_keeps_the_bracket(
    build, players, admin, flush_calls, monkeypatch
):
    tournament = build(PAUSED, phase_two_state=True)
    before = _state(tournament)
    taken = before['teams'][0].name
    monkeypatch.setattr(
        tournament_repository,
        'find_active_team_by_name',
        lambda tournament_id, name: None,
    )

    with _Probe(tournament.id) as probe:
        result = _create(tournament, admin, players[5], name=taken)

    _assert_refused_and_kept(
        tournament,
        before,
        probe,
        flush_calls,
        result,
        'A team with this name already exists in this tournament.',
        flushed=True,
    )


def test_create_team_with_restart_reraises_an_unknown_integrity_error_and_keeps_the_bracket(
    build, players, admin, flush_calls, monkeypatch
):
    tournament = build(PAUSED, phase_two_state=True)
    before = _state(tournament)

    def fail(team):
        raise IntegrityError('insert', {}, Exception('boom'))

    monkeypatch.setattr(tournament_repository, 'create_team', fail)

    with _Probe(tournament.id) as probe:
        with pytest.raises(IntegrityError):
            _create(tournament, admin, players[5])

    _assert_unlocked(tournament)
    assert len(flush_calls) == 1
    assert probe.seen == []
    assert _state(tournament) == before


def test_create_team_still_refuses_an_unregistered_captain_outside_restart(
    build, players, admin
):
    tournament = build(PAUSED)
    before = _state(tournament)

    with _Probe(tournament.id) as probe:
        result = team_service.create_team(
            tournament.id, f'Plain team {generate_uuid7()}', players[5].id
        )

    assert result.is_err()
    assert result.unwrap_err() == (
        'The team captain must be a registered participant in this tournament.'
    )
    _assert_unlocked(tournament)
    assert probe.seen == []
    assert _state(tournament) == before
    assert before['tournament'].tournament_status is PAUSED
