"""
tests.integration.services.lan_tournament.test_ffa_undersized_pool
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

In FFA double elimination, a removed finisher shrinks the losers pool.
When that alone pushes a lobby below the minimum size, the lobby is
generated undersized, with a board notice and an audit entry. A pool
that is too small without any removal stays refused. A losers pool of
one contestant gets no lobby: the contestant has a bye and carries over.
"""

from datetime import datetime, UTC
from itertools import count

import pytest

from byceps.database import db
from byceps.services.lan_tournament import (
    tournament_log_service,
    tournament_match_service,
    tournament_participant_service,
    tournament_repository,
    tournament_score_service,
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
from byceps.services.lan_tournament.models.score_ordering import ScoreOrdering
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


PARTY_ID = PartyID('lan-party-ffa-undersized-pool')

_counter = count(1)

KINDS = ['plain_ffa', 'highscore_ffa']
WHEN = ['before_confirm', 'after_confirm']
EVENT = 'bracket-lobby-undersized'
BYE_EVENT = 'bracket-lobby-bye'


@pytest.fixture(scope='module')
def party(make_party, make_brand):
    brand = make_brand('ffaundersizedpoolbrand', 'FFA Undersized Brand')
    return make_party(brand, PARTY_ID, 'LAN Party FFA Undersized Pool')


@pytest.fixture(scope='module')
def players(make_user):
    return [make_user(f'FfaUndersizedPlayer{i}') for i in range(16)]


@pytest.fixture(scope='module')
def admin(make_user):
    return make_user('FfaUndersizedAdmin')


@pytest.fixture
def make_de(party, players, admin):
    """Return a started FFA DE tournament; the two WB lobbies are open."""
    created = []

    def _make(
        kind,
        *,
        minimum=4,
        cut=2,
        size=8,
        maximum=4,
        double=True,
        generate=True,
    ):
        common = {
            'contestant_type': ContestantType.SOLO,
            'point_table': [5, 3, 2, 1],
            'group_size_min': minimum,
            'group_size_max': maximum,
            'advancement_count': cut,
        }
        mode = (
            EliminationMode.DOUBLE_ELIMINATION
            if double
            else EliminationMode.SINGLE_ELIMINATION
        )
        if kind == 'plain_ffa':
            args = {
                **common,
                'game_format': GameFormat.FREE_FOR_ALL,
                'elimination_mode': mode,
                'tournament_status': TournamentStatus.REGISTRATION_CLOSED,
                'max_players': 16,
            }
        else:
            args = {
                **common,
                'game_format': GameFormat.HIGHSCORE,
                'elimination_mode': EliminationMode.NONE,
                'score_ordering': ScoreOrdering.HIGHER_IS_BETTER,
                'tournament_status': TournamentStatus.ONGOING,
                'playoff_game_format': GameFormat.FREE_FOR_ALL,
                'playoff_elimination_mode': mode,
                'playoff_qualifier_count': 8,
                'playoff_release_mode': PlayoffReleaseMode.AUTOMATIC,
            }
        tournament, _ = tournament_service.create_tournament(
            PARTY_ID, f'FFA Undersized {next(_counter)}', **args
        ).unwrap()
        created.append(tournament)
        for user in players[:size]:
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
        if not generate:
            return tournament
        if kind == 'plain_ffa':
            board = tournament_seeding_service.get_board(
                tournament.id, initiator_id=admin.id
            ).unwrap()
            tournament_seeding_service.generate_from_seeding(
                tournament.id,
                expected_version=board.version,
                initiator_id=admin.id,
            ).unwrap()
            tournament_service.change_status(
                tournament.id, TournamentStatus.ONGOING, admin.id
            ).unwrap()
        else:
            participants = (
                tournament_repository.get_participants_for_tournament(
                    tournament.id
                )
            )
            for value, participant in enumerate(participants, start=1):
                tournament_score_service.submit_score(
                    tournament.id, value * 10, participant_id=participant.id
                ).unwrap()
            tournament_score_service.close_leaderboard(
                tournament.id, initiator_id=admin.id
            ).unwrap()
        assert len(_lobbies(tournament, bracket=_pool(double))) >= 2
        return tournament

    yield _make
    db.session.rollback()
    for tournament in created:
        if tournament_repository.find_tournament(tournament.id) is not None:
            tournament_service.delete_tournament(tournament.id)


def _pool(double):
    return Bracket.WINNERS if double else None


def _lobbies(tournament, *, bracket=Bracket.WINNERS):
    return sorted(
        (
            m
            for m in tournament_repository.get_matches_for_tournament(
                tournament.id
            )
            if m.bracket is bracket
            and (m.phase == 2 or not tournament.playoff_game_format)
        ),
        key=lambda m: (m.round or 0, m.group_order or 0),
    )


def _members(match):
    """Return the lobby's contestants by recorded placement, then ID."""
    contestants = tournament_match_service.get_contestants_for_match(match.id)
    return [
        str(c.participant_id)
        for c in sorted(
            contestants,
            key=lambda c: (
                c.placement is None,
                c.placement or 0,
                str(c.participant_id),
            ),
        )
    ]


def _play(match, admin):
    """Place the lobby by contestant ID and confirm it."""
    match = tournament_match_service.get_match(match.id)
    order = sorted(_members(match))
    tournament_match_service.set_ffa_placements(
        match.id, {cid: i + 1 for i, cid in enumerate(order)}
    ).unwrap()
    tournament_match_service.confirm_ffa_match(match.id, admin.id).unwrap()


def _remove(tournament, participant_id, admin):
    tournament_participant_service.admin_remove_participant(
        tournament.id,
        TournamentParticipantID(participant_id),
        initiator=admin,
    ).unwrap()


def _round_of(tournament, bracket, round_number):
    return [
        m
        for m in _lobbies(tournament, bracket=bracket)
        if m.round == round_number
    ]


def _first_round(tournament, bracket=Bracket.WINNERS):
    lobbies = _lobbies(tournament, bracket=bracket)
    return [m for m in lobbies if m.round == lobbies[0].round]


def _setup(make_de, admin, kind, when, *, minimum=4, cut=2, victim_place=0):
    """Play round 0 of a DE tournament, with one removal at `when`."""
    tournament = make_de(kind, minimum=minimum, cut=cut)
    lobbies = _first_round(tournament)
    victim = sorted(_members(lobbies[0]))[victim_place]
    if when == 'before_confirm':
        _remove(tournament, victim, admin)
    for lobby in lobbies:
        _play(lobby, admin)
    if when == 'after_confirm':
        _remove(tournament, victim, admin)
    return tournament, victim


def _generate_from_draft(tournament, admin):
    target = tournament_seeding_service.prepare_ffa_round_draft(
        tournament.id, pool=Bracket.WINNERS, initiator_id=admin.id
    ).unwrap()
    board = tournament_seeding_service.get_board(
        tournament.id, target, initiator_id=admin.id
    ).unwrap()
    generated = tournament_seeding_service.generate_from_seeding(
        tournament.id,
        target,
        expected_version=board.version,
        initiator_id=admin.id,
    )
    return target, board, generated


def _undersized_entries(tournament):
    return [
        e
        for e in tournament_log_service.get_entries_for_tournament(
            tournament.id
        )
        if e.event_type == EVENT
    ]


@pytest.mark.parametrize('when', WHEN)
@pytest.mark.parametrize('kind', KINDS)
def test_the_draft_generates_an_undersized_losers_lobby(
    make_de, admin, kind, when
):
    tournament, victim = _setup(make_de, admin, kind, when)

    target, board, generated = _generate_from_draft(tournament, admin)

    assert generated.is_ok(), generated.unwrap_err()
    (losers,) = _lobbies(tournament, bracket=Bracket.LOSERS)
    assert len(_members(losers)) == 3
    assert victim not in _members(losers)
    (winners,) = _round_of(tournament, Bracket.WINNERS, 1)
    assert len(_members(winners)) == 4
    assert victim not in _members(winners)


@pytest.mark.parametrize('when', WHEN)
@pytest.mark.parametrize('kind', KINDS)
def test_the_board_shows_a_notice_and_the_audit_log_an_entry(
    make_de, admin, kind, when
):
    tournament, _victim = _setup(make_de, admin, kind, when)

    target, board, generated = _generate_from_draft(tournament, admin)
    assert generated.is_ok(), generated.unwrap_err()
    after = tournament_seeding_service.get_board(
        tournament.id, target, initiator_id=admin.id
    ).unwrap()

    for shown in (board, after):
        (pool,) = shown.undersized
        assert pool.pool is Bracket.LOSERS
        assert (pool.count, pool.lobbies, pool.minimum) == (3, (3,), 4)
    (entry,) = _undersized_entries(tournament)
    assert entry.initiator_id == admin.id
    assert entry.data == {
        'pool': 'LB',
        'round': pool.round_number,
        'count': 3,
        'lobbies': [3],
        'minimum': 4,
    }


@pytest.mark.parametrize('when', WHEN)
@pytest.mark.parametrize('kind', KINDS)
def test_advance_ffa_round_generates_the_undersized_lobby(
    make_de, admin, kind, when
):
    tournament, _victim = _setup(make_de, admin, kind, when)

    advanced = tournament_match_service.advance_ffa_round(
        tournament.id, pool=Bracket.WINNERS, initiator_id=admin.id
    )

    assert advanced.is_ok(), advanced.unwrap_err()
    (losers,) = _lobbies(tournament, bracket=Bracket.LOSERS)
    assert len(_members(losers)) == 3
    assert len(_undersized_entries(tournament)) == 1


@pytest.mark.parametrize('kind', KINDS)
def test_without_a_removal_a_too_small_pool_stays_refused(make_de, admin, kind):
    # Cut 3 of two lobbies of 4: the winners pool holds 6, i.e. two lobbies
    # of 3, below the minimum of 4. Nobody was removed.
    tournament = make_de(kind, minimum=4, cut=3)
    for lobby in _first_round(tournament):
        _play(lobby, admin)

    advanced = tournament_match_service.advance_ffa_round(
        tournament.id, pool=Bracket.WINNERS, initiator_id=admin.id
    )
    _target, _board, generated = _generate_from_draft(tournament, admin)

    expected = tournament_match_service.FFA_LOBBY_BELOW_MINIMUM_ERROR
    assert advanced.is_err() and advanced.unwrap_err() == expected
    assert generated.is_err() and generated.unwrap_err() == expected
    assert not _undersized_entries(tournament)


def _bye_entries(tournament):
    return [
        e
        for e in tournament_log_service.get_entries_for_tournament(
            tournament.id
        )
        if e.event_type == BYE_EVENT
    ]


def _entries_of(tournament, participant_id):
    """Return every lobby entry of the contestant, in any round."""
    return [
        (m.bracket, m.round)
        for m in tournament_repository.get_matches_for_tournament(tournament.id)
        for c in tournament_match_service.get_contestants_for_match(m.id)
        if str(c.participant_id) == participant_id
    ]


def _screen_name(players, tournament, participant_id):
    user_id = next(
        p.user_id
        for p in tournament_repository.get_participants_for_tournament(
            tournament.id
        )
        if str(p.id) == participant_id
    )
    return next(u.screen_name for u in players if u.id == user_id)


def _advance(tournament, admin, pool=Bracket.WINNERS):
    return tournament_match_service.advance_ffa_round(
        tournament.id, pool=pool, initiator_id=admin.id
    )


def _setup_three_drops(make_de, admin, kind, when, *, minimum=3):
    """Play round 0 (cut 2), with three of the four contestants who drop
    removed at `when`. Return the tournament and the one left."""
    tournament = make_de(kind, minimum=minimum, cut=2)
    lobbies = _first_round(tournament)
    first, second = (sorted(_members(m)) for m in lobbies)
    removed = [first[2], first[3], second[3]]
    if when == 'before_confirm':
        for participant_id in removed:
            _remove(tournament, participant_id, admin)
    for lobby in lobbies:
        _play(lobby, admin)
    if when == 'after_confirm':
        for participant_id in removed:
            _remove(tournament, participant_id, admin)
    return tournament, second[2]


@pytest.mark.parametrize('via', ['advance', 'draft'])
@pytest.mark.parametrize('when', WHEN)
def test_a_lone_losers_contestant_gets_a_bye(make_de, admin, when, via):
    # Minimum 2, cut 3: the losers pool holds 2. The removed finisher of
    # lobby 0 leaves it with 1, who gets no lobby but a bye.
    tournament, victim = _setup(
        make_de, admin, 'plain_ffa', when, minimum=2, cut=3
    )
    bye = _members(_first_round(tournament)[1])[3]

    if via == 'advance':
        result = _advance(tournament, admin)
    else:
        _target, _board, result = _generate_from_draft(tournament, admin)

    assert result.is_ok(), result.unwrap_err()
    assert not _lobbies(tournament, bracket=Bracket.LOSERS)
    placed = [
        pid
        for m in _round_of(tournament, Bracket.WINNERS, 1)
        for pid in _members(m)
    ]
    assert len(placed) == 6
    assert bye not in placed and victim not in placed
    (entry,) = _bye_entries(tournament)
    assert entry.initiator_id == admin.id
    assert entry.data == {'pool': 'LB', 'round': 0, 'contestant': bye}
    assert not _undersized_entries(tournament)
    # The bye is no played lobby: no entry, so no points.
    assert _entries_of(tournament, bye) == [(Bracket.WINNERS, 0)]


@pytest.mark.parametrize('via', ['advance', 'draft'])
@pytest.mark.parametrize('when', WHEN)
@pytest.mark.parametrize('kind', KINDS)
def test_a_pool_left_with_one_removal_survivor_gets_a_bye(
    make_de, admin, kind, when, via
):
    tournament, bye = _setup_three_drops(make_de, admin, kind, when)

    if via == 'advance':
        result = _advance(tournament, admin)
    else:
        _target, _board, result = _generate_from_draft(tournament, admin)

    assert result.is_ok(), result.unwrap_err()
    assert not _lobbies(tournament, bracket=Bracket.LOSERS)
    (winners,) = _round_of(tournament, Bracket.WINNERS, 1)
    assert len(_members(winners)) == 4 and bye not in _members(winners)
    (entry,) = _bye_entries(tournament)
    assert entry.data == {'pool': 'LB', 'round': 0, 'contestant': bye}
    assert not _undersized_entries(tournament)
    assert _entries_of(tournament, bye) == [(Bracket.WINNERS, 0)]


@pytest.mark.parametrize('kind', KINDS)
def test_the_board_names_the_contestant_with_the_bye(
    make_de, admin, players, kind
):
    tournament, bye = _setup_three_drops(make_de, admin, kind, 'after_confirm')
    name = _screen_name(players, tournament, bye)

    target, board, generated = _generate_from_draft(tournament, admin)
    assert generated.is_ok(), generated.unwrap_err()
    after = tournament_seeding_service.get_board(
        tournament.id, target, initiator_id=admin.id
    ).unwrap()

    assert board.byes == (name,)
    assert after.byes == (name,)


def test_no_bye_notice_when_every_pool_has_a_lobby(make_de, admin):
    tournament, _victim = _setup(
        make_de, admin, 'plain_ffa', 'after_confirm', minimum=2, cut=2
    )

    _target, board, generated = _generate_from_draft(tournament, admin)

    assert generated.is_ok(), generated.unwrap_err()
    assert board.byes == ()
    assert not _bye_entries(tournament)


def test_a_losers_draft_for_one_contestant_is_not_offered(make_de, admin):
    # Two lobbies of 4, cut 2: the losers round holds 2. One of them is
    # removed after the confirm, which leaves a lone survivor.
    tournament = make_de('plain_ffa', minimum=2, cut=2)
    lobbies = _first_round(tournament)
    for lobby in lobbies:
        _play(lobby, admin)
    dropped = [*_members(lobbies[0])[2:], *_members(lobbies[1])[2:]]
    for participant_id in dropped[:2]:
        _remove(tournament, participant_id, admin)
    assert _advance(tournament, admin).is_ok()
    (losers,) = _lobbies(tournament, bracket=Bracket.LOSERS)
    _play(losers, admin)
    _remove(tournament, _members(losers)[0], admin)
    survivor = _members(losers)[1]

    again = [_advance(tournament, admin, Bracket.LOSERS) for _ in range(2)]
    drafted = tournament_seeding_service.prepare_ffa_round_draft(
        tournament.id, pool=Bracket.LOSERS, initiator_id=admin.id
    )

    expected = tournament_match_service.FFA_LONE_SURVIVOR_ERROR
    assert [r.unwrap_err() for r in again] == [expected, expected]
    assert drafted.is_err() and drafted.unwrap_err() == expected
    assert len(_lobbies(tournament, bracket=Bracket.LOSERS)) == 1
    assert not _bye_entries(tournament)

    # The survivor stays in the losers pool: the next winners advance
    # takes them into the next losers round exactly once.
    (winners,) = _round_of(tournament, Bracket.WINNERS, 1)
    _play(winners, admin)
    assert _advance(tournament, admin).is_ok()
    (next_losers,) = _round_of(tournament, Bracket.LOSERS, 1)
    assert len(_members(next_losers)) == 3
    assert _members(next_losers).count(survivor) == 1


@pytest.mark.parametrize('when', WHEN)
def test_the_bye_contestant_joins_the_next_losers_round_once(
    make_de, admin, when
):
    # Minimum 2: a highscore playoff needs a minimum above its cut, and
    # the next winners round of 2 would then be below it.
    tournament, bye = _setup_three_drops(
        make_de, admin, 'plain_ffa', when, minimum=2
    )
    assert _advance(tournament, admin).is_ok()
    assert not _lobbies(tournament, bracket=Bracket.LOSERS)
    (winners,) = _round_of(tournament, Bracket.WINNERS, 1)
    assert len(_members(winners)) == 4
    _play(winners, admin)
    dropped = _members(winners)[2:]

    advanced = _advance(tournament, admin)

    assert advanced.is_ok(), advanced.unwrap_err()
    (losers,) = _round_of(tournament, Bracket.LOSERS, 0)
    assert sorted(_members(losers)) == sorted([*dropped, bye])
    assert _entries_of(tournament, bye).count((Bracket.LOSERS, 0)) == 1
    assert len(_round_of(tournament, Bracket.WINNERS, 2)) == 1
    assert len(_bye_entries(tournament)) == 1
    assert not _undersized_entries(tournament)


@pytest.mark.parametrize('lobby', [0, 1])
@pytest.mark.parametrize('when', WHEN)
@pytest.mark.parametrize('kind', KINDS)
def test_the_bye_contestant_joins_the_grand_final_when_it_comes_first(
    make_de, admin, kind, when, lobby
):
    # Lobby 1 is the bye contestant's: removing its winner must not
    # re-rank round 0 and promote the bye contestant out of the pool.
    tournament, bye = _setup_three_drops(make_de, admin, kind, when)
    assert _advance(tournament, admin).is_ok()
    (winners,) = _round_of(tournament, Bracket.WINNERS, 1)
    leaving = sorted(_members(_first_round(tournament)[lobby]))[0]
    assert leaving in _members(winners)
    _remove(tournament, leaving, admin)
    _play(winners, admin)
    playing = _members(winners)

    advanced = _advance(tournament, admin)
    generated = tournament_match_service.generate_ffa_grand_final(
        tournament.id, initiator_id=admin.id
    )

    assert advanced.unwrap() == 'grand_final_eligible'
    assert generated.is_ok(), generated.unwrap_err()
    (final,) = _lobbies(tournament, bracket=Bracket.GRAND_FINAL)
    assert sorted(_members(final)) == sorted([*playing, bye])
    assert not _lobbies(tournament, bracket=Bracket.LOSERS)


def test_a_layout_with_a_lone_losers_pool_gets_a_bye(make_de, admin):
    # Five contestants fall into lobbies of 3 and 2. With a cut of 2 only
    # the lobby of 3 drops one: the losers pool holds 1, without a removal.
    tournament = make_de('plain_ffa', minimum=2, cut=2, size=5)
    lobbies = _first_round(tournament)
    assert sorted(len(_members(m)) for m in lobbies) == [2, 3]
    for lobby in lobbies:
        _play(lobby, admin)
    (bye,) = [
        pid for m in lobbies if len(_members(m)) == 3 for pid in _members(m)[2:]
    ]

    advanced = _advance(tournament, admin)

    assert advanced.is_ok(), advanced.unwrap_err()
    assert not _lobbies(tournament, bracket=Bracket.LOSERS)
    (entry,) = _bye_entries(tournament)
    assert entry.data == {'pool': 'LB', 'round': 0, 'contestant': bye}
    assert not _undersized_entries(tournament)


def _drive_double_elimination(tournament, admin, *, remove_leaver):
    """Play and advance until the Grand Final is next or a pool stalls.

    With `remove_leaver`, the last place of the first losers lobby leaves
    once it is played: an eliminated contestant, in no later pool.
    """
    removed = False
    for _step in range(12):
        for bracket in (Bracket.WINNERS, Bracket.LOSERS):
            for lobby in _lobbies(tournament, bracket=bracket):
                if lobby.confirmed_by is None:
                    _play(lobby, admin)
        losers = _lobbies(tournament, bracket=Bracket.LOSERS)
        if remove_leaver and not removed and losers:
            last = max(
                tournament_match_service.get_contestants_for_match(
                    losers[0].id
                ),
                key=lambda c: c.placement or 0,
            )
            _remove(tournament, str(last.participant_id), admin)
            removed = True
        for pool in (Bracket.WINNERS, Bracket.LOSERS):
            advanced = _advance(tournament, admin, pool)
            if advanced.is_ok() and advanced.unwrap() == 'grand_final_eligible':
                return 'eligible'
            if advanced.is_ok():
                break
        else:
            return 'stalled'
    return 'limit'


def _wb_undersized_entries(tournament):
    return [
        e for e in _undersized_entries(tournament) if e.data['pool'] == 'WB'
    ]


def test_an_eliminated_leaver_does_not_stall_the_bracket(make_de, admin):
    tournament = make_de('plain_ffa', minimum=4, cut=2, size=16)

    outcome = _drive_double_elimination(tournament, admin, remove_leaver=True)

    assert outcome == 'eligible'


def test_an_eliminated_leaver_keeps_the_natural_label(make_de, admin):
    tournament = make_de('plain_ffa', minimum=3, cut=2, size=16)

    outcome = _drive_double_elimination(tournament, admin, remove_leaver=True)

    assert outcome == 'eligible'
    entries = _wb_undersized_entries(tournament)
    assert entries
    assert all(e.data.get('reason') == 'natural_shortfall' for e in entries)


def _leaver_of_a_single_track(tournament, kind, admin):
    """Play round 0 of a single-track tournament; remove an eliminated one.

    A highscore tournament seats 8 of its 10 players: a non-qualifier
    leaves. Otherwise the last place of lobby 0 does.
    """
    lobbies = _first_round(tournament, bracket=None)
    for lobby in lobbies:
        _play(lobby, admin)
    if kind == 'plain_ffa':
        leaver = _members(lobbies[0])[-1]
    else:
        seated = {pid for m in lobbies for pid in _members(m)}
        leaver = next(
            str(p.id)
            for p in tournament_repository.get_participants_for_tournament(
                tournament.id
            )
            if str(p.id) not in seated
        )
    _remove(tournament, leaver, admin)


@pytest.mark.parametrize('kind', KINDS)
def test_a_leaver_does_not_unlock_a_single_track_shortfall(
    make_de, admin, kind
):
    # Two lobbies of 4 and a cut of 1 leave a final of 2, below the minimum
    # of 3. That is natural whoever left: a leaver neither causes nor
    # relabels it.
    tournament = make_de(
        kind,
        minimum=3,
        maximum=4,
        cut=1,
        size=8 if kind == 'plain_ffa' else 10,
        double=False,
    )
    _leaver_of_a_single_track(tournament, kind, admin)

    advanced = _advance(tournament, admin, None)

    assert advanced.is_ok(), advanced.unwrap_err()
    (final,) = _round_of(tournament, None, 1)
    assert len(_members(final)) == 2
    (entry,) = _undersized_entries(tournament)
    assert entry.data == {
        'pool': 'SE',
        'round': 1,
        'count': 2,
        'lobbies': [2],
        'minimum': 3,
        'reason': 'natural_shortfall',
    }


def test_a_final_below_the_minimum_shows_a_board_notice(make_de, admin):
    tournament = make_de('plain_ffa', minimum=3, maximum=4, cut=1, double=False)
    for lobby in _first_round(tournament, bracket=None):
        _play(lobby, admin)

    target = tournament_seeding_service.prepare_ffa_round_draft(
        tournament.id, pool=None, initiator_id=admin.id
    ).unwrap()
    board = tournament_seeding_service.get_board(
        tournament.id, target, initiator_id=admin.id
    ).unwrap()

    (pool,) = board.undersized
    assert pool.pool is None and pool.natural_shortfall
    assert (pool.count, pool.lobbies, pool.minimum) == (2, (2,), 3)


def test_a_single_track_setup_that_stalls_is_refused_before_round_one(
    make_de, admin
):
    # 12 players, lobbies of 4 and a cut of 2: round 1 would hold 6, two
    # lobbies of 3, below the minimum of 4. More than one lobby is no final.
    tournament = make_de(
        'plain_ffa',
        minimum=4,
        maximum=4,
        cut=2,
        size=12,
        double=False,
        generate=False,
    )
    board = tournament_seeding_service.get_board(
        tournament.id, initiator_id=admin.id
    ).unwrap()

    generated = tournament_seeding_service.generate_from_seeding(
        tournament.id,
        expected_version=board.version,
        initiator_id=admin.id,
    )

    assert generated == Err(tournament_seeding_service.ERR_PROBLEMS)
    assert tournament_seeding_service.PROBLEM_FFA_STALLS in board.problems
    assert not tournament_repository.get_matches_for_tournament(tournament.id)


def test_a_removal_never_makes_a_one_contestant_lobby(make_de, admin):
    # Four lobbies of 2, a cut of 1. Both contestants of lobby 0 leave
    # after the confirm, so no one takes its slot: three contestants for
    # lobbies of at most 2.
    tournament = make_de(
        'plain_ffa', minimum=2, maximum=2, cut=1, size=8, double=False
    )
    lobbies = _first_round(tournament, bracket=None)
    for lobby in lobbies:
        _play(lobby, admin)
    for member in sorted(_members(lobbies[0])):
        _remove(tournament, member, admin)

    advanced = _advance(tournament, admin, None)

    assert advanced.is_err()
    assert advanced.unwrap_err() == (
        tournament_match_service.FFA_LOBBY_BELOW_MINIMUM_ERROR
    )
    assert not _round_of(tournament, None, 1)
    assert not _undersized_entries(tournament)
