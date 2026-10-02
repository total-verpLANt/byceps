"""
tests.integration.services.lan_tournament.test_ffa_cut_generation_guard
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

A round-0 FFA generation that needs a cut the tournament lacks is
refused with a static msgid and leaves no state behind.
"""

from dataclasses import replace
from datetime import datetime, UTC
from itertools import count

import pytest

from byceps.database import db
from byceps.services.lan_tournament import (
    tournament_log_service,
    tournament_match_service,
    tournament_orga_service,
    tournament_qualification_service,
    tournament_repository,
    tournament_score_service,
    tournament_seeding_service,
    tournament_service,
)
from byceps.services.lan_tournament.models import ContestantType
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.playoff import PlayoffReleaseMode
from byceps.services.lan_tournament.models.score_ordering import ScoreOrdering
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipant,
    TournamentParticipantID,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.lan_tournament.tournament_domain_service import (
    FFA_CUT_REQUIRED_MSGID,
)
from byceps.util.uuid import generate_uuid7

from tests.helpers import http_client, log_in_user


BASE_URL = 'http://www.acmecon.test/lan-tournaments'

_counter = count(1)


@pytest.fixture(scope='module')
def players(make_user):
    return [make_user(f'FfaCutGuardPlayer{i}') for i in range(8)]


@pytest.fixture(scope='module')
def orga(make_user):
    user = make_user('FfaCutGuardOrga')
    log_in_user(user.id)
    return user


@pytest.fixture
def make_tournament(party, players):
    created = []

    def _make(*, participants, **overrides):
        kwargs = {
            'contestant_type': ContestantType.SOLO,
            'game_format': GameFormat.FREE_FOR_ALL,
            'elimination_mode': EliminationMode.SINGLE_ELIMINATION,
            'tournament_status': TournamentStatus.REGISTRATION_CLOSED,
            'max_players': 16,
            'group_size_min': 2,
            'group_size_max': 4,
            'advancement_count': 2,
            'point_table': [10, 6, 3, 1],
        }
        kwargs.update(overrides)
        result = tournament_service.create_tournament(
            party.id, f'FFA Cut Guard {next(_counter)}', **kwargs
        )
        assert result.is_ok(), result.unwrap_err()
        tournament, _ = result.unwrap()
        created.append(tournament)
        for user in players[:participants]:
            tournament_repository.create_participant(
                TournamentParticipant(
                    id=TournamentParticipantID(_uuid()),
                    user_id=user.id,
                    tournament_id=tournament.id,
                    substitute_player=False,
                    team_id=None,
                    created_at=datetime.now(UTC),
                )
            )
        db.session.commit()
        return tournament

    yield _make
    db.session.rollback()
    for tournament in created:
        if tournament_repository.find_tournament(tournament.id) is not None:
            tournament_service.delete_tournament(tournament.id)


def _uuid():
    return generate_uuid7()


def _set_cut(tournament, advancement_count):
    """Store the cut, bypassing the service rules."""
    current = tournament_repository.get_tournament(tournament.id)
    tournament_repository.update_tournament(
        replace(current, advancement_count=advancement_count)
    )
    db.session.commit()


_UPDATE_FIELDS = (
    'name',
    'game',
    'description',
    'image_url',
    'ruleset',
    'start_time',
    'min_players',
    'max_players',
    'min_teams',
    'max_teams',
    'min_players_in_team',
    'max_players_in_team',
    'contestant_type',
    'game_format',
    'elimination_mode',
    'score_ordering',
    'point_table',
    'advancement_count',
    'group_size_min',
    'group_size_max',
    'points_carry_to_losers',
)


def _repair_cut(tournament, advancement_count):
    """Set the cut through the service, the way an orga does."""
    current = tournament_repository.get_tournament(tournament.id)
    fields = {name: getattr(current, name) for name in _UPDATE_FIELDS}
    fields['advancement_count'] = advancement_count
    return tournament_service.update_tournament(tournament.id, **fields)


def _matches(tournament):
    db.session.rollback()
    return tournament_repository.get_matches_for_tournament(tournament.id)


def _released_at(tournament):
    db.session.rollback()
    return tournament_repository.get_tournament(
        tournament.id
    ).playoff_released_at


def _highscore(
    make_tournament, players, release_mode=PlayoffReleaseMode.MANUAL
):
    tournament = make_tournament(
        participants=5,
        game_format=GameFormat.HIGHSCORE,
        elimination_mode=EliminationMode.NONE,
        tournament_status=TournamentStatus.ONGOING,
        score_ordering=ScoreOrdering.HIGHER_IS_BETTER,
        group_size_max=2,
        advancement_count=1,
        playoff_game_format=GameFormat.FREE_FOR_ALL,
        playoff_elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        playoff_qualifier_count=4,
        playoff_release_mode=release_mode,
    )
    _set_cut(tournament, None)
    for participant, score in zip(
        tournament_repository.get_participants_for_tournament(tournament.id),
        [100, 90, 80, 70, 60],
        strict=True,
    ):
        tournament_score_service.submit_score(
            tournament.id, score, participant_id=participant.id
        ).unwrap()
    closed = tournament_score_service.close_leaderboard(
        tournament.id, initiator_id=players[0].id
    )
    assert closed.is_ok(), closed.unwrap_err()
    return tournament


def _draft_version(tournament):
    board = tournament_seeding_service.ensure_playoff_draft(tournament.id)
    assert board.is_ok(), board.unwrap_err()
    return board.unwrap().version


def test_plain_ffa_generation_without_a_cut_is_refused_then_works(
    make_tournament, players
):
    tournament = make_tournament(participants=8, group_size_max=4)
    _set_cut(tournament, None)

    refused = tournament_match_service.generate_ffa_round(
        tournament.id, initiator_id=players[0].id
    )

    assert refused.is_err()
    assert refused.unwrap_err() == FFA_CUT_REQUIRED_MSGID
    assert not _matches(tournament)

    repaired = _repair_cut(tournament, 2)
    assert repaired.is_ok(), repaired.unwrap_err()
    generated = tournament_match_service.generate_ffa_round(
        tournament.id, initiator_id=players[0].id
    )

    assert generated.is_ok(), generated
    assert len(_matches(tournament)) == 2


def test_single_lobby_se_without_a_cut_generates_and_completes(
    make_tournament, players
):
    tournament = make_tournament(participants=4, group_size_max=4)
    _set_cut(tournament, None)

    generated = tournament_match_service.generate_ffa_round(
        tournament.id, initiator_id=players[0].id
    )

    assert generated.is_ok(), generated
    (match,) = _matches(tournament)
    placements = {
        str(c.participant_id): place
        for place, c in enumerate(
            tournament_match_service.get_contestants_for_match(match.id),
            start=1,
        )
    }
    tournament_match_service.set_ffa_placements(match.id, placements).unwrap()
    tournament_service.change_status(
        tournament.id, TournamentStatus.ONGOING, players[0].id
    ).unwrap()
    confirmed = tournament_match_service.confirm_ffa_match(
        match.id, players[0].id
    )

    assert confirmed.is_ok(), confirmed
    db.session.rollback()
    assert (
        tournament_repository.get_tournament(tournament.id).tournament_status
        == TournamentStatus.COMPLETED
    )


def test_double_elimination_single_lobby_without_a_cut_is_refused(
    make_tournament, players
):
    tournament = make_tournament(
        participants=4,
        group_size_max=4,
        elimination_mode=EliminationMode.DOUBLE_ELIMINATION,
    )
    _set_cut(tournament, None)

    refused = tournament_match_service.generate_ffa_round(
        tournament.id, initiator_id=players[0].id
    )

    assert refused.is_err()
    assert refused.unwrap_err() == FFA_CUT_REQUIRED_MSGID
    assert not _matches(tournament)


def test_playoff_release_without_a_cut_is_refused_then_works(
    make_tournament, players
):
    tournament = _highscore(make_tournament, players)
    version = _draft_version(tournament)

    refused = tournament_qualification_service.release_playoffs(
        tournament.id, expected_version=version, initiator_id=players[0].id
    )

    assert refused.is_err()
    assert refused.unwrap_err() == FFA_CUT_REQUIRED_MSGID
    assert _released_at(tournament) is None
    assert not _matches(tournament)

    repaired = _repair_cut(tournament, 1)
    assert repaired.is_ok(), repaired.unwrap_err()
    released = tournament_qualification_service.release_playoffs(
        tournament.id,
        expected_version=_draft_version(tournament),
        initiator_id=players[0].id,
    )

    assert released.is_ok(), released
    assert _released_at(tournament) is not None


def test_advance_rounds_are_not_affected(make_tournament, players):
    tournament = make_tournament(participants=8, group_size_max=4)
    tournament_match_service.generate_ffa_round(
        tournament.id, initiator_id=players[0].id
    ).unwrap()
    tournament_service.change_status(
        tournament.id, TournamentStatus.ONGOING, players[0].id
    ).unwrap()
    for match in _matches(tournament):
        ranked = sorted(
            str(c.participant_id)
            for c in tournament_match_service.get_contestants_for_match(
                match.id
            )
        )
        tournament_match_service.set_ffa_placements(
            match.id, {cid: place for place, cid in enumerate(ranked, 1)}
        ).unwrap()
        tournament_match_service.confirm_ffa_match(
            match.id, players[0].id
        ).unwrap()

    drafted = tournament_seeding_service.prepare_ffa_round_draft(
        tournament.id, initiator_id=players[0].id
    )
    assert drafted.is_ok(), drafted
    board = tournament_seeding_service.get_board(
        tournament.id, drafted.unwrap()
    ).unwrap()
    advanced = tournament_seeding_service.generate_from_seeding(
        tournament.id,
        drafted.unwrap(),
        expected_version=board.version,
        initiator_id=players[0].id,
    )

    assert advanced.is_ok(), advanced
    assert {m.round for m in _matches(tournament)} == {0, 1}


def test_release_route_names_the_missing_cut(
    site_app, orga, make_tournament, players
):
    tournament = _highscore(make_tournament, players)
    tournament_orga_service.assign_orga(
        tournament.id, orga.id, orga.id
    ).unwrap()
    version = _draft_version(tournament)

    with http_client(site_app, user_id=orga.id) as client:
        response = client.post(
            f'{BASE_URL}/orga/tournaments/{tournament.id}'
            '/qualification/release',
            data={'version': str(version)},
        )
        with client.session_transaction() as session:
            flashes = session.pop('_flashes', None) or []

    assert response.status_code == 302
    assert FFA_CUT_REQUIRED_MSGID in ' | '.join(
        str(message) for _, message in flashes
    )
    assert _released_at(tournament) is None


def test_auto_release_without_a_cut_refuses_cleanly(make_tournament, players):
    tournament = _highscore(
        make_tournament, players, PlayoffReleaseMode.AUTOMATIC
    )

    # The close triggers the automatic release; its Err is ignored.
    again = tournament_qualification_service.try_auto_release(
        tournament.id, triggered_by=players[0].id
    )

    assert again.is_err()
    assert again.unwrap_err() == FFA_CUT_REQUIRED_MSGID
    assert _released_at(tournament) is None
    assert not _matches(tournament)
    event_types = {
        entry.event_type
        for entry in tournament_log_service.get_entries_for_tournament(
            tournament.id
        )
    }
    assert 'playoffs-released' not in event_types
