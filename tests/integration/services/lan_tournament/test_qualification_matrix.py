"""
tests.integration.services.lan_tournament.test_qualification_matrix
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

One test per row of PRD 28.4 (groups), each driven through the three
playoff flows: RR groups to SE, RR groups to DE, highscore to FFA.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, UTC
from itertools import count
from pathlib import Path

from babel.messages.pofile import read_po
import pytest
from sqlalchemy import select

import byceps
from byceps.database import db
from byceps.services.lan_tournament import (
    tournament_match_service,
    tournament_qualification_domain_service as qualification_domain,
    tournament_qualification_repository,
    tournament_qualification_service,
    tournament_repository,
    tournament_score_service,
    tournament_seeding_service,
    tournament_service,
)
from byceps.services.lan_tournament.dbmodels.tournament_log_entry import (
    DbTournamentLogEntry,
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
from byceps.services.party.models import PartyID
from byceps.util.uuid import generate_uuid7


PARTY_ID = PartyID('lan-party-2024-qualification-matrix')

_counter = count(1)

FLOWS = ['groups-se', 'groups-de', 'highscore-ffa']

TieKind = qualification_domain.TieKind

# The PRD sentence of 20.3 and 28.4, "Gleichstand mit Einfluss".
PRD_TIE_BLOCKER = (
    'Qualifikation kann aufgrund eines Gleichstands nicht automatisch '
    'bestimmt werden. Orgaentscheidung erforderlich.'
)

# Group pair of (stronger, weaker) strength indices that draws, per kind.
_DRAW_PAIR = {
    'clear': None,
    'below_cut': (2, 3),
    'above_cut': (0, 1),
    'across_cut': (1, 2),
}

# Leaderboard values of the eight participants, per kind; the cut is 4.
_VALUES = {
    'clear': [100, 90, 80, 70, 60, 50, 40, 30],
    'below_cut': [100, 90, 80, 70, 50, 50, 30, 20],
    'above_cut': [100, 80, 80, 70, 60, 50, 40, 30],
    'across_cut': [100, 90, 80, 70, 70, 50, 40, 30],
}

_TIE_KINDS = {
    'clear': None,
    'below_cut': TieKind.HARMLESS,
    'above_cut': TieKind.SEEDING,
    'across_cut': TieKind.CUT,
}


@dataclass(frozen=True)
class Played:
    """A tournament with all but the last result entered."""

    tournament: object
    order: dict[str, list[str]]
    certain: frozenset[str]
    tied: tuple[str, ...]
    tie_scope: str | None
    tie_kind: TieKind | None
    finish: Callable[[], None]
    kind: str

    def qualifiers_after(self, decided: list[str]) -> set[str]:
        """Return who qualifies once the tie is decided in this order."""
        if self.kind == 'across_cut':
            return set(self.certain) | {decided[0]}
        return set(self.certain)


@pytest.fixture(scope='module')
def party(make_party, make_brand):
    brand = make_brand('qualificationmatrixbrand', 'Qualification Matrix Brand')
    return make_party(brand, PARTY_ID, 'LAN Party 2024 Qualification Matrix')


@pytest.fixture(scope='module')
def users(make_user):
    return [make_user(f'QualificationMatrixUser{i}') for i in range(8)]


@pytest.fixture
def make_played(party, users):
    created = []

    def _create(flow, release_mode, **overrides):
        if flow == 'highscore-ffa':
            args = dict(
                game_format=GameFormat.HIGHSCORE,
                elimination_mode=EliminationMode.NONE,
                score_ordering=ScoreOrdering.HIGHER_IS_BETTER,
                tournament_status=TournamentStatus.ONGOING,
                playoff_game_format=GameFormat.FREE_FOR_ALL,
                playoff_elimination_mode=EliminationMode.SINGLE_ELIMINATION,
                playoff_qualifier_count=4,
                point_table=[5, 3, 2, 1],
                group_size_min=3,
                group_size_max=4,
                advancement_count=2,
            )
        else:
            args = dict(
                game_format=GameFormat.ONE_V_ONE,
                elimination_mode=EliminationMode.ROUND_ROBIN,
                tournament_status=TournamentStatus.REGISTRATION_CLOSED,
                playoff_game_format=GameFormat.ONE_V_ONE,
                playoff_elimination_mode=(
                    EliminationMode.DOUBLE_ELIMINATION
                    if flow == 'groups-de'
                    else EliminationMode.SINGLE_ELIMINATION
                ),
                playoff_group_count=2,
                playoff_qualifiers_per_group=2,
            )
        args.update(overrides)
        result = tournament_service.create_tournament(
            PARTY_ID,
            f'Qualification Matrix Tournament {next(_counter)}',
            contestant_type=ContestantType.SOLO,
            playoff_release_mode=release_mode,
            **args,
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
        if flow != 'highscore-ffa':
            generated = tournament_match_service.generate_round_robin_bracket(
                tournament.id
            )
            assert generated.is_ok(), generated.unwrap_err()
            started = tournament_service.change_status(
                tournament.id, TournamentStatus.ONGOING, users[0].id
            )
            assert started.is_ok(), started.unwrap_err()
        return tournament

    def _make(flow, release_mode, kind, **overrides):
        tournament = _create(flow, release_mode, **overrides)
        if flow == 'highscore-ffa':
            return _play_highscore(tournament, users, kind)
        return _play_groups(tournament, users[0], kind)

    yield _make
    db.session.rollback()
    for tournament in created:
        if tournament_repository.find_tournament(tournament.id) is not None:
            tournament_service.delete_tournament(tournament.id)


def _groups(tournament):
    """Return the phase 1 matches of each group, with their contestants."""
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


def _play_groups(tournament, admin, kind):
    """Enter every group result but the last; the stronger id wins.

    Strength is the position of the participant ID in its group, sorted.
    Group 0 holds the tie of `kind`; the other group is clear.
    """
    plan = []
    order: dict[str, list[str]] = {}
    for group, matches in sorted(_groups(tournament).items()):
        members = sorted({str(i) for _, ids in matches for i in ids})
        assert len(members) == 4
        order[f'group:{group}'] = members
        draw = _DRAW_PAIR[kind] if group == 0 else None
        for match, ids in matches:
            stronger, weaker = sorted(ids, key=str)
            pair = (members.index(str(stronger)), members.index(str(weaker)))
            margin = group + 1
            scores = {stronger: margin, weaker: margin if pair == draw else 0}
            plan.append((match, scores))
    *entered, (last_match, last_scores) = plan
    for match, scores in entered:
        confirmed = tournament_match_service.admin_set_and_confirm_match(
            match.id, admin.id, scores
        )
        assert confirmed.is_ok(), confirmed.unwrap_err()

    def finish():
        confirmed = tournament_match_service.admin_set_and_confirm_match(
            last_match.id, admin.id, last_scores
        )
        assert confirmed.is_ok(), confirmed.unwrap_err()

    first = order['group:0']
    certain = {cid for members in order.values() for cid in members[:2]}
    tied = {
        'clear': (),
        'below_cut': (first[2], first[3]),
        'above_cut': (first[0], first[1]),
        'across_cut': (first[1], first[2]),
    }[kind]
    if kind == 'across_cut':
        certain.discard(first[1])
    return Played(
        tournament=tournament,
        order=order,
        certain=frozenset(certain),
        tied=tied,
        tie_scope='group:0' if tied else None,
        tie_kind=_TIE_KINDS[kind],
        finish=finish,
        kind=kind,
    )


def _play_highscore(tournament, users, kind):
    """Submit every value; closing the leaderboard is the last result."""
    by_user = {
        p.user_id: str(p.id)
        for p in tournament_repository.get_participants_for_tournament(
            tournament.id
        )
    }
    ids = [by_user[u.id] for u in users]
    for participant_id, value in zip(ids, _VALUES[kind], strict=True):
        submitted = tournament_score_service.submit_score(
            tournament.id,
            value,
            participant_id=TournamentParticipantID(participant_id),
        )
        assert submitted.is_ok(), submitted.unwrap_err()

    def finish():
        closed = tournament_score_service.close_leaderboard(
            tournament.id, initiator_id=users[0].id
        )
        assert closed.is_ok(), closed.unwrap_err()

    tied = {
        'clear': (),
        'below_cut': tuple(ids[4:6]),
        'above_cut': tuple(ids[1:3]),
        'across_cut': tuple(ids[3:5]),
    }[kind]
    certain = set(ids[:3] if kind == 'across_cut' else ids[:4])
    return Played(
        tournament=tournament,
        order={'leaderboard': ids},
        certain=frozenset(certain),
        tied=tied,
        tie_scope='leaderboard' if tied else None,
        tie_kind=_TIE_KINDS[kind],
        finish=finish,
        kind=kind,
    )


def _qualification(tournament):
    result = tournament_qualification_service.get_qualification(tournament.id)
    assert result.is_ok(), result.unwrap_err()
    return result.unwrap()


def _phase_two(tournament):
    return [
        m
        for m in tournament_repository.get_matches_for_tournament(tournament.id)
        if m.phase == 2
    ]


def _phase_two_contestants(tournament):
    return {
        str(c.participant_id)
        for match in _phase_two(tournament)
        for c in tournament_repository.get_contestants_for_match(match.id)
    }


def _released(tournament):
    return tournament_repository.get_tournament(tournament.id)


def _log_entries(tournament, event_type):
    with db.session.no_autoflush:
        return db.session.scalars(
            select(DbTournamentLogEntry).filter_by(
                tournament_id=tournament.id, event_type=event_type
            )
        ).all()


def _decide(played, admin, *, reverse=True):
    """Decide the tie by the orga; return the order that was saved."""
    decided = list(played.tied)
    if reverse:
        decided.reverse()
    result = tournament_qualification_service.save_decision(
        played.tournament.id,
        played.tie_scope,
        decided,
        reason='Decided by the orga',
        initiator_id=admin.id,
    )
    assert result.is_ok(), result.unwrap_err()
    return decided


def _release(tournament, admin):
    board = tournament_seeding_service.ensure_playoff_draft(tournament.id)
    assert board.is_ok(), board.unwrap_err()
    return tournament_qualification_service.release_playoffs(
        tournament.id,
        expected_version=board.unwrap().version,
        initiator_id=admin.id,
    )


@pytest.mark.parametrize('flow', FLOWS)
def test_unambiguous_placement(flow, make_played):
    played = make_played(flow, PlayoffReleaseMode.MANUAL, 'clear')
    played.finish()

    state = _qualification(played.tournament)

    assert state.blockers == ()
    assert len(state.rankings) == len(played.order)
    for scope, order in played.order.items():
        (ranking,) = [r for r in state.rankings if r.scope == scope]
        assert [e.contestant_id for e in ranking.entries] == order
        assert [e.rank for e in ranking.entries] == list(
            range(1, len(order) + 1)
        )
        assert not any(e.shared for e in ranking.entries)
        assert ranking.ties == ()


@pytest.mark.parametrize('flow', FLOWS)
def test_unambiguous_qualification(flow, make_played, users):
    played = make_played(flow, PlayoffReleaseMode.MANUAL, 'clear')
    played.finish()

    state = _qualification(played.tournament)

    assert state.ready
    assert state.blockers == ()
    assert state.qualifiers is not None
    assert {q.contestant_id for q in state.qualifiers} == played.certain
    cut = 4 if flow == 'highscore-ffa' else 2
    assert all(q.rank <= cut for q in state.qualifiers)
    assert len(state.qualifiers) == 4
    assert state.released_at is None
    assert _release(played.tournament, users[0]).is_ok()
    assert _phase_two_contestants(played.tournament) == played.certain


@pytest.mark.parametrize('flow', FLOWS)
def test_tie_without_effect_on_qualification(flow, make_played, users):
    played = make_played(flow, PlayoffReleaseMode.MANUAL, 'below_cut')
    played.finish()

    state = _qualification(played.tournament)

    (tie,) = [t for r in state.rankings for t in r.ties]
    assert tie.kind is TieKind.HARMLESS
    assert set(tie.contestant_ids) == set(played.tied)
    assert not tie.decided
    assert state.blockers == ()
    assert state.ready
    assert {q.contestant_id for q in state.qualifiers} == played.certain
    assert (
        tournament_qualification_repository.find_decision(
            played.tournament.id, played.tie_scope
        )
        is None
    )
    assert _release(played.tournament, users[0]).is_ok()
    assert _phase_two_contestants(played.tournament) == played.certain


@pytest.mark.parametrize('kind', ['across_cut', 'above_cut'])
@pytest.mark.parametrize('flow', FLOWS)
def test_tie_with_effect_on_qualification(flow, kind, make_played, users):
    played = make_played(flow, PlayoffReleaseMode.AUTOMATIC, kind)
    tournament = played.tournament

    played.finish()

    state = _qualification(tournament)
    (block,) = state.blockers
    assert block.scope == played.tie_scope
    assert set(block.contestant_ids) == set(played.tied)
    assert block.kind is played.tie_kind
    assert not block.decided
    assert not state.ready
    assert state.qualifiers is None
    # No automatic decision: nothing is released, nothing is generated.
    assert not _phase_two(tournament)
    assert _released(tournament).playoff_released_at is None
    assert (
        tournament_seeding_service.ensure_playoff_draft(
            tournament.id
        ).unwrap_err()
        == tournament_seeding_service.ERR_PLAYOFF_NOT_READY
    )
    forced = tournament_qualification_service.release_playoffs(
        tournament.id, expected_version=1, initiator_id=users[0].id
    )
    assert forced.unwrap_err() == (
        tournament_seeding_service.ERR_PLAYOFF_NOT_READY
    )
    auto = tournament_qualification_service.try_auto_release(
        tournament.id, triggered_by=users[0].id
    )
    assert auto.unwrap() is False
    assert not _phase_two(tournament)
    assert _log_entries(tournament, 'playoffs-released') == []
    assert _log_entries(tournament, 'qualification-tie-decided') == []

    blank = tournament_qualification_service.save_decision(
        tournament.id,
        played.tie_scope,
        list(played.tied),
        reason='  ',
        initiator_id=users[0].id,
    )
    assert blank.unwrap_err() == 'Please give a reason for the decision.'
    assert _qualification(tournament).blockers == (block,)

    decided = _decide(played, users[0])

    after = _qualification(tournament)
    assert after.ready
    assert after.blockers == ()
    final = played.qualifiers_after(decided)
    assert {q.contestant_id for q in after.qualifiers} == final
    (entry,) = _log_entries(tournament, 'qualification-tie-decided')
    assert entry.initiator_id == users[0].id
    assert entry.data['scope'] == played.tie_scope
    assert entry.data['reason'] == 'Decided by the orga'
    # The decision made the automatic release due.
    assert _phase_two_contestants(tournament) == final


@pytest.mark.parametrize('kind', ['clear', 'below_cut', 'across_cut'])
@pytest.mark.parametrize('flow', FLOWS)
def test_automatic_playoff_release(flow, kind, make_played, users):
    played = make_played(flow, PlayoffReleaseMode.AUTOMATIC, kind)
    tournament = played.tournament
    assert not _phase_two(tournament)
    assert _released(tournament).playoff_released_at is None
    assert not _qualification(tournament).ready

    played.finish()

    if kind == 'across_cut':
        assert not _phase_two(tournament)
        assert _released(tournament).playoff_released_at is None
        final = played.qualifiers_after(_decide(played, users[0]))
    else:
        final = set(played.certain)

    found = _released(tournament)
    assert found.playoff_released_at is not None
    assert found.playoff_released_by is None
    assert _phase_two_contestants(tournament) == final
    assert _qualification(tournament).released_at is not None
    (entry,) = _log_entries(tournament, 'playoffs-released')
    assert entry.initiator_id is None
    assert entry.data['mode'] == 'automatic'
    assert entry.data['match_count'] == len(_phase_two(tournament))
    again = tournament_qualification_service.try_auto_release(
        tournament.id, triggered_by=users[0].id
    )
    assert again.unwrap() is False
    assert len(_log_entries(tournament, 'playoffs-released')) == 1


@pytest.mark.parametrize('kind', ['clear', 'across_cut'])
@pytest.mark.parametrize('flow', FLOWS)
def test_manual_playoff_release(flow, kind, make_played, users):
    played = make_played(flow, PlayoffReleaseMode.MANUAL, kind)
    tournament = played.tournament
    played.finish()
    final = set(played.certain)
    if kind == 'across_cut':
        final = played.qualifiers_after(_decide(played, users[0]))

    state = _qualification(tournament)
    assert state.ready
    assert {q.contestant_id for q in state.qualifiers} == final
    # The qualified are computed, but the orga has not released yet.
    assert state.released_at is None
    assert not _phase_two(tournament)
    waiting = tournament_qualification_service.try_auto_release(
        tournament.id, triggered_by=users[0].id
    )
    assert waiting.unwrap() is False
    assert _released(tournament).playoff_released_at is None
    assert _log_entries(tournament, 'playoffs-released') == []

    released = _release(tournament, users[1])

    assert released.is_ok(), released.unwrap_err()
    assert released.unwrap() == len(_phase_two(tournament)) > 0
    found = _released(tournament)
    assert found.playoff_released_at is not None
    assert found.playoff_released_by == users[1].id
    assert _phase_two_contestants(tournament) == final
    (entry,) = _log_entries(tournament, 'playoffs-released')
    assert entry.initiator_id == users[1].id
    assert entry.data['mode'] == 'manual'


def test_tie_blocker_msgid_is_the_prd_sentence():
    path = (
        Path(byceps.__file__).parent / 'translations/de/LC_MESSAGES/messages.po'
    )
    with path.open('rb') as file:
        catalog = read_po(file, locale='de')

    message = catalog.get(tournament_match_service.QUALIFICATION_TIE_ERROR)

    assert message is not None
    assert message.string == PRD_TIE_BLOCKER


def test_tie_with_effect_in_a_phase_two_lobby_blocks_with_the_prd_sentence(
    make_played, users
):
    played = make_played(
        'highscore-ffa',
        PlayoffReleaseMode.AUTOMATIC,
        'clear',
        point_table=[1, 1],
        group_size_min=2,
        group_size_max=2,
        advancement_count=1,
    )
    tournament = played.tournament
    played.finish()
    lobbies = _phase_two(tournament)
    assert len(lobbies) == 2
    for lobby in lobbies:
        placements = {
            str(c.participant_id): place
            for place, c in enumerate(
                tournament_repository.get_contestants_for_match(lobby.id),
                start=1,
            )
        }
        assert tournament_match_service.set_ffa_placements(
            lobby.id, placements
        ).is_ok()
        assert tournament_match_service.confirm_ffa_match(
            lobby.id, users[0].id
        ).is_ok()

    blocked = tournament_match_service.advance_ffa_round(
        tournament.id, initiator_id=users[0].id
    )

    assert blocked.unwrap_err() == (
        tournament_match_service.QUALIFICATION_TIE_ERROR
    )
    assert len(_phase_two(tournament)) == 2
    assert _released(tournament).tournament_status is TournamentStatus.ONGOING
