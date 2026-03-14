"""
tests.unit.services.lan_tournament.test_bracket_serialization
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Unit tests for bracket JSON serialization helpers in
``lan_tournament_view_helpers``.
"""

from datetime import datetime

from byceps.services.lan_tournament.models.bracket import Bracket
from byceps.services.lan_tournament.models.contestant_type import (
    ContestantType,
)
from byceps.services.lan_tournament.models.tournament import (
    Tournament,
    TournamentID,
)
from byceps.services.lan_tournament.models.tournament_match import (
    TournamentMatch,
    TournamentMatchID,
)
from byceps.services.lan_tournament.models.tournament_match_to_contestant import (
    TournamentMatchToContestant,
    TournamentMatchToContestantID,
)
from byceps.services.lan_tournament.models.tournament_mode import (
    TournamentMode,
)
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipantID,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.lan_tournament.models.tournament_team import (
    TournamentTeam,
    TournamentTeamID,
)
from byceps.services.lan_tournament.lan_tournament_view_helpers import (
    _resolve_contestant_name,
    serialize_bracket_json,
)
from byceps.services.party.models import PartyID
from byceps.services.user.models.user import User, UserID

from tests.helpers import generate_uuid


NOW = datetime(2025, 6, 15, 14, 0, 0)
PARTY_ID = PartyID('test-party-2025')
TOURNAMENT_ID = TournamentID(generate_uuid())


# -------------------------------------------------------------------- #
# factory helpers
# -------------------------------------------------------------------- #


def _make_tournament(
    *,
    mode: TournamentMode = TournamentMode.SINGLE_ELIMINATION,
    contestant_type: ContestantType = ContestantType.SOLO,
    status: TournamentStatus = TournamentStatus.ONGOING,
) -> Tournament:
    return Tournament(
        id=TOURNAMENT_ID,
        party_id=PARTY_ID,
        name='Test Cup',
        game='TestGame',
        description=None,
        image_url=None,
        ruleset=None,
        start_time=NOW,
        created_at=NOW,
        min_players=None,
        max_players=None,
        min_teams=None,
        max_teams=None,
        min_players_in_team=None,
        max_players_in_team=None,
        contestant_type=contestant_type,
        tournament_status=status,
        tournament_mode=mode,
    )


def _make_match(
    *,
    match_id: TournamentMatchID | None = None,
    round_num: int = 1,
    match_order: int = 0,
    bracket: Bracket | None = None,
    confirmed: bool = False,
    next_match_id: TournamentMatchID | None = None,
    loser_next_match_id: TournamentMatchID | None = None,
) -> TournamentMatch:
    if match_id is None:
        match_id = TournamentMatchID(generate_uuid())
    return TournamentMatch(
        id=match_id,
        tournament_id=TOURNAMENT_ID,
        group_order=None,
        match_order=match_order,
        round=round_num,
        next_match_id=next_match_id,
        confirmed_by=UserID(generate_uuid()) if confirmed else None,
        created_at=NOW,
        bracket=bracket,
        loser_next_match_id=loser_next_match_id,
    )


def _make_contestant(
    *,
    match_id: TournamentMatchID,
    team_id: TournamentTeamID | None = None,
    participant_id: TournamentParticipantID | None = None,
    score: int | None = None,
) -> TournamentMatchToContestant:
    return TournamentMatchToContestant(
        id=TournamentMatchToContestantID(generate_uuid()),
        tournament_match_id=match_id,
        team_id=team_id,
        participant_id=participant_id,
        score=score,
        created_at=NOW,
    )


def _make_team(
    *,
    team_id: TournamentTeamID | None = None,
    name: str = 'Team Alpha',
) -> TournamentTeam:
    if team_id is None:
        team_id = TournamentTeamID(generate_uuid())
    return TournamentTeam(
        id=team_id,
        tournament_id=TOURNAMENT_ID,
        name=name,
        tag=None,
        description=None,
        image_url=None,
        captain_user_id=UserID(generate_uuid()),
        join_code=None,
        created_at=NOW,
    )


def _make_user(
    *,
    user_id: UserID | None = None,
    screen_name: str | None = 'PlayerOne',
) -> User:
    if user_id is None:
        user_id = UserID(generate_uuid())
    return User(
        id=user_id,
        screen_name=screen_name,
        initialized=True,
        suspended=False,
        deleted=False,
        avatar_url='',
    )


# -------------------------------------------------------------------- #
# _resolve_contestant_name
# -------------------------------------------------------------------- #


def test_resolve_contestant_name_team():
    """Team contestant resolves to team name."""
    team = _make_team(name='Fraggers United')
    teams_by_id = {team.id: team}
    match_id = TournamentMatchID(generate_uuid())
    contestant = _make_contestant(match_id=match_id, team_id=team.id)

    result = _resolve_contestant_name(contestant, teams_by_id, {})
    assert result == 'Fraggers United'


def test_resolve_contestant_name_solo():
    """Solo contestant resolves to user screen_name."""
    pid = TournamentParticipantID(generate_uuid())
    user = _make_user(screen_name='xXSlayerXx')
    participants_by_id = {pid: user}
    match_id = TournamentMatchID(generate_uuid())
    contestant = _make_contestant(match_id=match_id, participant_id=pid)

    result = _resolve_contestant_name(contestant, {}, participants_by_id)
    assert result == 'xXSlayerXx'


def test_resolve_contestant_name_missing():
    """Unknown contestant resolves to 'TBD'."""
    match_id = TournamentMatchID(generate_uuid())
    contestant = _make_contestant(match_id=match_id)

    result = _resolve_contestant_name(contestant, {}, {})
    assert result == 'TBD'


def test_resolve_contestant_name_solo_no_screen_name():
    """Solo contestant without screen_name falls back to str(user.id)."""
    pid = TournamentParticipantID(generate_uuid())
    user = _make_user(screen_name=None)
    participants_by_id = {pid: user}
    match_id = TournamentMatchID(generate_uuid())
    contestant = _make_contestant(match_id=match_id, participant_id=pid)

    result = _resolve_contestant_name(contestant, {}, participants_by_id)
    assert result == str(user.id)


# -------------------------------------------------------------------- #
# serialize_bracket_json — single elimination
# -------------------------------------------------------------------- #


def test_serialize_bracket_json_single_elimination():
    """SE tournament with 4 matches produces expected JSON structure."""
    tournament = _make_tournament(mode=TournamentMode.SINGLE_ELIMINATION)

    # Build a mini SE bracket: 2 semis -> 1 final
    final_id = TournamentMatchID(generate_uuid())
    semi1 = _make_match(round_num=1, match_order=0, next_match_id=final_id)
    semi2 = _make_match(round_num=1, match_order=1, next_match_id=final_id)
    final = _make_match(match_id=final_id, round_num=2, match_order=0, confirmed=True)
    third_place = _make_match(round_num=2, match_order=1)

    pid_a = TournamentParticipantID(generate_uuid())
    pid_b = TournamentParticipantID(generate_uuid())
    pid_c = TournamentParticipantID(generate_uuid())
    pid_d = TournamentParticipantID(generate_uuid())

    user_a = _make_user(screen_name='Alice')
    user_b = _make_user(screen_name='Bob')
    user_c = _make_user(screen_name='Charlie')
    user_d = _make_user(screen_name='Diana')

    participants_by_id = {
        pid_a: user_a,
        pid_b: user_b,
        pid_c: user_c,
        pid_d: user_d,
    }

    match_data = [
        {
            'match': semi1,
            'contestants': [
                _make_contestant(match_id=semi1.id, participant_id=pid_a, score=2),
                _make_contestant(match_id=semi1.id, participant_id=pid_b, score=1),
            ],
        },
        {
            'match': semi2,
            'contestants': [
                _make_contestant(match_id=semi2.id, participant_id=pid_c, score=3),
                _make_contestant(match_id=semi2.id, participant_id=pid_d, score=0),
            ],
        },
        {
            'match': final,
            'contestants': [
                _make_contestant(match_id=final.id, participant_id=pid_a, score=5),
                _make_contestant(match_id=final.id, participant_id=pid_c, score=4),
            ],
        },
        {
            'match': third_place,
            'contestants': [],
        },
    ]

    result = serialize_bracket_json(
        tournament, match_data, {}, participants_by_id, {}, {}
    )

    # Tournament metadata
    assert result['tournament']['id'] == str(TOURNAMENT_ID)
    assert result['tournament']['name'] == 'Test Cup'
    assert result['tournament']['mode'] == 'SINGLE_ELIMINATION'
    assert result['tournament']['contestant_type'] == 'SOLO'
    assert result['tournament']['status'] == 'ONGOING'

    # Matches
    assert len(result['matches']) == 4

    # Verify semi1
    m0 = result['matches'][0]
    assert m0['id'] == str(semi1.id)
    assert m0['round'] == 1
    assert m0['match_order'] == 0
    assert m0['next_match_id'] == str(final_id)
    assert m0['confirmed'] is False
    assert len(m0['contestants']) == 2
    assert m0['contestants'][0]['name'] == 'Alice'
    assert m0['contestants'][0]['score'] == 2
    assert m0['contestants'][1]['name'] == 'Bob'

    # Verify final is confirmed
    m2 = result['matches'][2]
    assert m2['confirmed'] is True

    # match_urls initialized to None for each match
    assert len(result['match_urls']) == 4
    for mid_key in result['match_urls']:
        assert result['match_urls'][mid_key] is None

    # hover_data
    assert result['hover_data'] == {'seats': {}, 'team_members': {}}


# -------------------------------------------------------------------- #
# serialize_bracket_json — double elimination
# -------------------------------------------------------------------- #


def test_serialize_bracket_json_double_elimination():
    """DE tournament produces WB/LB/GF grouped matches."""
    tournament = _make_tournament(mode=TournamentMode.DOUBLE_ELIMINATION)

    gf_id = TournamentMatchID(generate_uuid())
    lb_final_id = TournamentMatchID(generate_uuid())

    wb_match = _make_match(
        round_num=1, match_order=0, bracket=Bracket.WINNERS,
        next_match_id=gf_id, loser_next_match_id=lb_final_id,
    )
    lb_match = _make_match(
        match_id=lb_final_id, round_num=1, match_order=0,
        bracket=Bracket.LOSERS, next_match_id=gf_id,
    )
    gf_match = _make_match(
        match_id=gf_id, round_num=2, match_order=0,
        bracket=Bracket.GRAND_FINAL,
    )

    pid_a = TournamentParticipantID(generate_uuid())
    pid_b = TournamentParticipantID(generate_uuid())
    user_a = _make_user(screen_name='Alpha')
    user_b = _make_user(screen_name='Bravo')
    participants_by_id = {pid_a: user_a, pid_b: user_b}

    match_data = [
        {
            'match': wb_match,
            'contestants': [
                _make_contestant(match_id=wb_match.id, participant_id=pid_a),
                _make_contestant(match_id=wb_match.id, participant_id=pid_b),
            ],
        },
        {
            'match': lb_match,
            'contestants': [
                _make_contestant(match_id=lb_match.id, participant_id=pid_b),
            ],
        },
        {
            'match': gf_match,
            'contestants': [],
        },
    ]

    result = serialize_bracket_json(
        tournament, match_data, {}, participants_by_id, {}, {}
    )

    assert result['tournament']['mode'] == 'DOUBLE_ELIMINATION'
    assert len(result['matches']) == 3

    # Verify bracket values
    brackets = [m['bracket'] for m in result['matches']]
    assert brackets == ['WB', 'LB', 'GF']

    # WB match has loser_next_match_id
    assert result['matches'][0]['loser_next_match_id'] == str(lb_final_id)
    # GF match has no next
    assert result['matches'][2]['next_match_id'] is None


# -------------------------------------------------------------------- #
# serialize_bracket_json — empty contestants
# -------------------------------------------------------------------- #


def test_serialize_bracket_json_empty_contestants():
    """Match with no contestants produces empty contestants list."""
    tournament = _make_tournament()

    empty_match = _make_match(round_num=1, match_order=0)
    match_data = [
        {
            'match': empty_match,
            'contestants': [],
        },
    ]

    result = serialize_bracket_json(tournament, match_data, {}, {}, {}, {})

    assert len(result['matches']) == 1
    assert result['matches'][0]['contestants'] == []


# -------------------------------------------------------------------- #
# serialize_bracket_json — hover data
# -------------------------------------------------------------------- #


def test_serialize_bracket_json_hover_data():
    """Hover data serializes seats keyed by participant_id (not user_id)."""
    tournament = _make_tournament(contestant_type=ContestantType.TEAM)

    team = _make_team(name='Aces')
    teams_by_id = {team.id: team}

    match = _make_match(round_num=1, match_order=0)
    match_data = [
        {
            'match': match,
            'contestants': [
                _make_contestant(match_id=match.id, team_id=team.id),
            ],
        },
    ]

    uid1 = UserID(generate_uuid())
    uid2 = UserID(generate_uuid())
    pid1 = TournamentParticipantID(generate_uuid())
    pid2 = TournamentParticipantID(generate_uuid())

    user1 = _make_user(user_id=uid1)
    user2 = _make_user(user_id=uid2)
    participants_by_id = {pid1: user1, pid2: user2}

    seats = {uid1: 'A1', uid2: 'B3'}
    team_members = {
        team.id: [('Player1', 'A1'), ('Player2', 'B3')],
    }

    result = serialize_bracket_json(
        tournament, match_data, teams_by_id, participants_by_id, seats, team_members
    )

    # Seats are now keyed by participant_id (for JS hover card lookup)
    assert result['hover_data']['seats'] == {
        str(pid1): 'A1',
        str(pid2): 'B3',
    }
    assert result['hover_data']['team_members'] == {
        str(team.id): [('Player1', 'A1'), ('Player2', 'B3')],
    }


# -------------------------------------------------------------------- #
# serialize_bracket_json — url_builder
# -------------------------------------------------------------------- #


def test_serialize_bracket_json_with_url_builder():
    """url_builder callable populates match_urls atomically."""
    tournament = _make_tournament()

    m1 = _make_match(round_num=1, match_order=0)
    m2 = _make_match(round_num=1, match_order=1)
    match_data = [
        {'match': m1, 'contestants': []},
        {'match': m2, 'contestants': []},
    ]

    result = serialize_bracket_json(
        tournament,
        match_data,
        {},
        {},
        {},
        {},
        url_builder=lambda m: f'/match/{m.id}',
    )

    assert result['match_urls'][str(m1.id)] == f'/match/{m1.id}'
    assert result['match_urls'][str(m2.id)] == f'/match/{m2.id}'


def test_serialize_bracket_json_url_builder_none_default():
    """Without url_builder, match_urls values are None."""
    tournament = _make_tournament()

    m = _make_match(round_num=1, match_order=0)
    match_data = [{'match': m, 'contestants': []}]

    result = serialize_bracket_json(tournament, match_data, {}, {}, {}, {})

    assert result['match_urls'][str(m.id)] is None


def test_serialize_bracket_json_url_builder_receives_match_object():
    """url_builder callable receives the TournamentMatch, not a dict."""
    tournament = _make_tournament()
    m = _make_match(round_num=1, match_order=0)
    match_data = [{'match': m, 'contestants': []}]

    received = []

    def capture_builder(match):
        received.append(match)
        return f'/m/{match.id}'

    serialize_bracket_json(
        tournament, match_data, {}, {}, {}, {},
        url_builder=capture_builder,
    )

    assert len(received) == 1
    assert received[0] is m
    assert isinstance(received[0], TournamentMatch)


# -------------------------------------------------------------------- #
# serialize_bracket_json — None-guard branches
# -------------------------------------------------------------------- #


def test_serialize_bracket_json_tournament_mode_none():
    """Tournament with mode=None serializes mode as None."""
    tournament = Tournament(
        id=TOURNAMENT_ID,
        party_id=PARTY_ID,
        name='Draft Cup',
        game='TestGame',
        description=None,
        image_url=None,
        ruleset=None,
        start_time=NOW,
        created_at=NOW,
        min_players=None,
        max_players=None,
        min_teams=None,
        max_teams=None,
        min_players_in_team=None,
        max_players_in_team=None,
        contestant_type=None,
        tournament_status=None,
        tournament_mode=None,
    )

    match_data = [
        {'match': _make_match(), 'contestants': []},
    ]

    result = serialize_bracket_json(tournament, match_data, {}, {}, {}, {})

    assert result['tournament']['mode'] is None
    assert result['tournament']['contestant_type'] == 'SOLO'  # default fallback
    assert result['tournament']['status'] is None


# -------------------------------------------------------------------- #
# serialize_bracket_json — empty match_data
# -------------------------------------------------------------------- #


def test_serialize_bracket_json_empty_match_data():
    """Empty match_data produces empty matches and match_urls."""
    tournament = _make_tournament()

    result = serialize_bracket_json(tournament, [], {}, {}, {}, {})

    assert result['matches'] == []
    assert result['match_urls'] == {}
    assert result['tournament']['name'] == 'Test Cup'


# -------------------------------------------------------------------- #
# serialize_bracket_json — score 0 vs None
# -------------------------------------------------------------------- #


def test_serialize_bracket_json_score_zero_vs_none():
    """Score 0 is preserved as 0, not conflated with None (unplayed)."""
    tournament = _make_tournament()
    m = _make_match(round_num=1, match_order=0)

    pid_a = TournamentParticipantID(generate_uuid())
    pid_b = TournamentParticipantID(generate_uuid())
    user_a = _make_user(screen_name='Winner')
    user_b = _make_user(screen_name='Loser')

    match_data = [
        {
            'match': m,
            'contestants': [
                _make_contestant(match_id=m.id, participant_id=pid_a, score=3),
                _make_contestant(match_id=m.id, participant_id=pid_b, score=0),
            ],
        },
    ]

    result = serialize_bracket_json(
        tournament, match_data, {}, {pid_a: user_a, pid_b: user_b}, {}, {}
    )

    scores = [c['score'] for c in result['matches'][0]['contestants']]
    assert scores == [3, 0]
    assert scores[1] == 0
    assert scores[1] is not None


# -------------------------------------------------------------------- #
# _resolve_contestant_name — additional branches
# -------------------------------------------------------------------- #


def test_resolve_contestant_name_team_id_not_in_lookup():
    """team_id set but absent from teams_by_id falls through to TBD."""
    match_id = TournamentMatchID(generate_uuid())
    phantom_team_id = TournamentTeamID(generate_uuid())
    contestant = _make_contestant(match_id=match_id, team_id=phantom_team_id)

    result = _resolve_contestant_name(contestant, {}, {})
    assert result == 'TBD'


def test_resolve_contestant_name_team_takes_precedence():
    """When both team_id and participant_id are set, team wins."""
    team = _make_team(name='Priority Team')
    pid = TournamentParticipantID(generate_uuid())
    user = _make_user(screen_name='ShouldNotAppear')
    match_id = TournamentMatchID(generate_uuid())

    contestant = TournamentMatchToContestant(
        id=TournamentMatchToContestantID(generate_uuid()),
        tournament_match_id=match_id,
        team_id=team.id,
        participant_id=pid,
        score=None,
        created_at=NOW,
    )

    result = _resolve_contestant_name(
        contestant, {team.id: team}, {pid: user}
    )
    assert result == 'Priority Team'


def test_resolve_contestant_name_team_id_missing_falls_to_participant():
    """team_id not in lookup but participant_id resolves to user."""
    phantom_team_id = TournamentTeamID(generate_uuid())
    pid = TournamentParticipantID(generate_uuid())
    user = _make_user(screen_name='FallbackPlayer')
    match_id = TournamentMatchID(generate_uuid())

    contestant = TournamentMatchToContestant(
        id=TournamentMatchToContestantID(generate_uuid()),
        tournament_match_id=match_id,
        team_id=phantom_team_id,
        participant_id=pid,
        score=None,
        created_at=NOW,
    )

    # teams_by_id does NOT contain phantom_team_id
    result = _resolve_contestant_name(contestant, {}, {pid: user})
    assert result == 'FallbackPlayer'
