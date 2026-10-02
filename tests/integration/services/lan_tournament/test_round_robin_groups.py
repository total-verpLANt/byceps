"""
tests.integration.services.lan_tournament.test_round_robin_groups
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from dataclasses import replace
from datetime import datetime, UTC
from itertools import count
from types import SimpleNamespace

import pytest

from byceps.database import db
from byceps.services.lan_tournament import (
    tournament_match_service,
    tournament_repository,
    tournament_seeding_service,
    tournament_service,
)
from byceps.services.lan_tournament.models import ContestantType
from byceps.services.lan_tournament.models.bracket import Bracket
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.playoff import PlayoffReleaseMode
from byceps.services.lan_tournament.models.seeding import SeedingFormat
from byceps.services.lan_tournament.models.tournament_match import (
    TournamentMatch,
    TournamentMatchID,
)
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipant,
    TournamentParticipantID,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.party.models import PartyID
from byceps.util.uuid import generate_uuid7


PARTY_ID = PartyID('lan-party-2024-rr-groups')

_counter = count(1)


@pytest.fixture(scope='module')
def party(make_party, make_brand):
    brand = make_brand('rrgroupsbrand', 'RR Groups Brand')
    return make_party(brand, PARTY_ID, 'LAN Party 2024 RR Groups')


@pytest.fixture(scope='module')
def users(make_user):
    return [make_user(f'RrGroupsUser{i}') for i in range(8)]


@pytest.fixture
def make_tournament(party, users):
    created = []

    def _make(participants=8, playoffs=True, **playoff_overrides):
        playoff_kwargs = {}
        if playoffs:
            playoff_kwargs = {
                'playoff_game_format': GameFormat.ONE_V_ONE,
                'playoff_elimination_mode': EliminationMode.SINGLE_ELIMINATION,
                'playoff_group_count': 2,
                'playoff_qualifiers_per_group': 2,
                'playoff_release_mode': PlayoffReleaseMode.MANUAL,
                **playoff_overrides,
            }
        result = tournament_service.create_tournament(
            PARTY_ID,
            f'RR Groups Tournament {next(_counter)}',
            contestant_type=ContestantType.SOLO,
            game_format=GameFormat.ONE_V_ONE,
            elimination_mode=EliminationMode.ROUND_ROBIN,
            tournament_status=TournamentStatus.REGISTRATION_CLOSED,
            **playoff_kwargs,
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


def _group_members(tournament_id):
    """Return the contestant id set of each group, keyed by group order."""
    matches = tournament_repository.get_matches_for_tournament(tournament_id)
    contestants = tournament_repository.get_contestants_for_tournament(
        tournament_id
    )
    members: dict[int | None, set[str]] = {}
    for match in matches:
        ids = members.setdefault(match.group_order, set())
        for c in contestants[match.id]:
            ids.add(str(c.participant_id))
    return matches, members


def _make_match(tournament_id, *, phase, round_=0, next_match_id=None):
    match = TournamentMatch(
        id=TournamentMatchID(generate_uuid7()),
        tournament_id=tournament_id,
        group_order=None,
        match_order=0,
        round=round_,
        next_match_id=next_match_id,
        confirmed_by=None,
        created_at=datetime.now(UTC),
        phase=phase,
    )
    tournament_repository.create_match(match)
    db.session.commit()
    return match


@pytest.mark.parametrize(
    ('participants', 'expected_counts'),
    [(8, {0: 6, 1: 6}), (7, {0: 3, 1: 6})],
)
def test_rr_groups_generate_per_group_schedules(
    make_tournament, participants, expected_counts
):
    tournament, ids = make_tournament(participants)

    result = tournament_match_service.generate_round_robin_bracket(
        tournament.id
    )

    assert result.is_ok(), result.unwrap_err()
    assert result.unwrap() == sum(expected_counts.values())
    matches, members = _group_members(tournament.id)
    assert {m.phase for m in matches} == {1}
    counts = {
        order: sum(1 for m in matches if m.group_order == order)
        for order in members
    }
    assert counts == expected_counts
    # The groups partition the roster: no contestant plays in two groups.
    assert members[0].isdisjoint(members[1])
    assert members[0] | members[1] == {str(i) for i in ids}
    for order, size in ((0, len(members[0])), (1, len(members[1]))):
        assert counts[order] == size * (size - 1) // 2


def test_rr_groups_regenerate_keeps_groups(make_tournament):
    tournament, _ = make_tournament(8)
    assert tournament_match_service.generate_round_robin_bracket(
        tournament.id
    ).is_ok()

    result = tournament_match_service.generate_round_robin_bracket(
        tournament.id, force_regenerate=True
    )

    assert result.is_ok(), result.unwrap_err()
    matches, members = _group_members(tournament.id)
    assert len(matches) == 12
    assert set(members) == {0, 1}


def test_plain_rr_generates_one_group_without_group_order(make_tournament):
    tournament, _ = make_tournament(4, playoffs=False)

    result = tournament_match_service.generate_round_robin_bracket(
        tournament.id
    )

    assert result.is_ok(), result.unwrap_err()
    matches, _ = _group_members(tournament.id)
    assert len(matches) == 6
    assert {m.group_order for m in matches} == {None}
    assert (
        tournament_match_service.validate_bracket_for_start(tournament.id) == []
    )


def test_validate_start_per_group(make_tournament):
    tournament, _ = make_tournament(8)
    assert tournament_match_service.generate_round_robin_bracket(
        tournament.id
    ).is_ok()

    # Two full groups of four are valid, although 8 contestants as one
    # group would need 28 matches.
    assert (
        tournament_match_service.validate_bracket_for_start(tournament.id) == []
    )

    group_one_match = next(
        m
        for m in tournament_repository.get_matches_for_tournament(tournament.id)
        if m.group_order == 1
    )
    tournament_repository.delete_contestants_for_match_flush(group_one_match.id)
    tournament_repository.delete_match_flush(group_one_match.id)
    db.session.commit()

    violations = tournament_match_service.validate_bracket_for_start(
        tournament.id
    )

    assert any(v.startswith('group 1:') for v in violations), violations
    assert not any(v.startswith('group 0:') for v in violations), violations


def test_phase_one_match_never_deciding(make_tournament):
    tournament, _ = make_tournament(4)
    phase_one = _make_match(tournament.id, phase=1)
    phase_two = _make_match(tournament.id, phase=2)

    assert not tournament_match_service.is_deciding_match(phase_one, tournament)
    assert tournament_match_service.is_deciding_match(phase_two, tournament)

    third_place = replace(phase_two, bracket=Bracket.THIRD_PLACE)
    assert not tournament_match_service.is_deciding_match(
        third_place, tournament
    )


def test_ffa_playoff_final_counts_phase_two_matches_only(make_tournament):
    tournament, _ = make_tournament(4)
    ffa_playoffs = replace(
        tournament,
        game_format=GameFormat.HIGHSCORE,
        elimination_mode=None,
        playoff_game_format=GameFormat.FREE_FOR_ALL,
        playoff_elimination_mode=EliminationMode.SINGLE_ELIMINATION,
    )
    phase_one = _make_match(tournament.id, phase=1, round_=0)
    phase_two = _make_match(tournament.id, phase=2, round_=0)

    assert tournament_match_service.is_deciding_match(phase_two, ffa_playoffs)
    assert not tournament_match_service.is_deciding_match(
        phase_one, ffa_playoffs
    )


def test_clear_bracket_is_scoped_to_a_phase(make_tournament):
    tournament, _ = make_tournament(4)
    assert tournament_match_service.generate_round_robin_bracket(
        tournament.id
    ).is_ok()
    phase_two = _make_match(tournament.id, phase=2)

    events = tournament_match_service.clear_bracket(tournament.id, phase=2)
    db.session.commit()

    assert [e.match_id for e in events] == [phase_two.id]
    remaining = tournament_repository.get_matches_for_tournament(tournament.id)
    assert len(remaining) == 2 and {m.phase for m in remaining} == {1}

    events = tournament_match_service.clear_bracket(tournament.id)
    db.session.commit()
    assert len(events) == 2
    assert not tournament_repository.get_matches_for_tournament(tournament.id)


def test_round_robin_seeding_format_carries_group_count(make_tournament):
    with_playoffs, ids = make_tournament(8)
    plain, _ = make_tournament(8, playoffs=False)
    roster = SimpleNamespace(ids=[str(i) for i in ids])

    grouped = tournament_seeding_service._format_for(with_playoffs, roster)
    ungrouped = tournament_seeding_service._format_for(plain, roster)

    assert grouped.unwrap() == (SeedingFormat.ROUND_ROBIN, 2)
    assert ungrouped.unwrap() == (SeedingFormat.ROUND_ROBIN, 1)


def test_create_refuses_300_playoff_groups(party):
    result = tournament_service.create_tournament(
        PARTY_ID,
        f'RR Groups Tournament {next(_counter)}',
        contestant_type=ContestantType.SOLO,
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.ROUND_ROBIN,
        tournament_status=TournamentStatus.REGISTRATION_CLOSED,
        playoff_game_format=GameFormat.ONE_V_ONE,
        playoff_elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        playoff_group_count=300,
        playoff_qualifiers_per_group=2,
        playoff_release_mode=PlayoffReleaseMode.MANUAL,
    )

    assert result.is_err()
    assert result.unwrap_err().msgid == 'At most %(max)s.'


def test_board_notices_a_shrunk_group_stage(make_tournament):
    tournament, _ = make_tournament(
        6,
        playoff_elimination_mode=EliminationMode.DOUBLE_ELIMINATION,
        playoff_group_count=4,
        playoff_qualifiers_per_group=1,
    )

    board = tournament_seeding_service.get_board(tournament.id).unwrap()

    assert board.notices == (
        tournament_seeding_service.NOTICE_FEWER_GROUPS,
        tournament_seeding_service.NOTICE_FEWER_QUALIFIERS,
        tournament_seeding_service.NOTICE_KNOCKOUT_FALLBACK,
    )
    assert list(board.notice_params) == [
        {'groups': 3, 'configured': 4},
        {'qualifiers': 3, 'configured': 4},
        {},
    ]
    assert board.problems == ()
    generated = tournament_seeding_service.generate_from_seeding(
        tournament.id, expected_version=board.version, initiator_id=None
    )
    assert generated.is_ok(), generated.unwrap_err()


def test_board_has_no_notices_when_the_roster_fills_the_groups(
    make_tournament,
):
    tournament, _ = make_tournament(8)

    board = tournament_seeding_service.get_board(tournament.id).unwrap()

    assert board.notices == ()
    assert board.notice_params == ()
