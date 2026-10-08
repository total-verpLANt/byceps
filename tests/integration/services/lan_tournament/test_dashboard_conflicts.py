from contextlib import contextmanager
from datetime import datetime, timedelta
from itertools import count
from uuid import uuid4

import pytest
from sqlalchemy import event

from byceps.database import db
from byceps.services.authn.session.models import CurrentUser
from byceps.services.lan_tournament import (
    permissions as _permissions,  # noqa: F401 -- registers the permissions
    tournament_dashboard_repository as repository,
    tournament_dashboard_service as service,
    tournament_repository as repo,
)
from byceps.services.lan_tournament.dbmodels.match import DbTournamentMatch
from byceps.services.lan_tournament.dbmodels.match_contestant import (
    DbTournamentMatchToContestant,
)
from byceps.services.lan_tournament.dbmodels.participant import (
    DbTournamentParticipant,
)
from byceps.services.lan_tournament.dbmodels.team import DbTournamentTeam
from byceps.services.lan_tournament.dbmodels.tournament_orga import (
    DbTournamentOrga,
)
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.operational_timing import (
    MatchDueEpisode,
    MatchDueEpisodeID,
)
from byceps.services.lan_tournament.models.tournament import (
    Tournament,
    TournamentID,
)
from byceps.services.lan_tournament.models.tournament_dashboard import (
    DashboardConflict,
    DashboardQuery,
    DashboardRowState,
    DashboardSettings,
)
from byceps.services.lan_tournament.models.tournament_match import (
    TournamentMatchID,
)
from byceps.services.lan_tournament.models.tournament_match_to_contestant import (
    TournamentMatchToContestantID,
)
from byceps.services.lan_tournament.models.tournament_orga import (
    TournamentOrgaID,
)
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipantID,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.lan_tournament.models.tournament_team import (
    TournamentTeamID,
)
from byceps.services.party.models import PartyID
from byceps.util.uuid import uuid7


NOW = datetime(2026, 10, 7, 12, 0, 0)
MINUTE_US = 60_000_000
CLOCK_START = NOW - timedelta(hours=3)
CLOCK_AT_NOW_US = 180 * MINUTE_US

SETTINGS = DashboardSettings(
    yellow_minutes=15,
    red_minutes=45,
    poll_seconds=30,
    page_size=50,
    threshold_source='deployment',
)

ONGOING = TournamentStatus.ONGOING
PAUSED = TournamentStatus.PAUSED
ONE_V_ONE = GameFormat.ONE_V_ONE
FREE_FOR_ALL = GameFormat.FREE_FOR_ALL
SINGLE = EliminationMode.SINGLE_ELIMINATION
ROUND_ROBIN = EliminationMode.ROUND_ROBIN

DUE = DashboardRowState.DUE


@pytest.fixture(scope='module')
def users(make_user):
    return [make_user(f'F03Conflict{i:02d}') for i in range(14)]


@pytest.fixture(scope='module')
def orga(make_user):
    return make_user('F03ConflictOrga')


@pytest.fixture(scope='module')
def confirmer(make_user):
    return make_user('F03ConflictConfirmer')


@pytest.fixture(scope='module')
def admin(make_admin):
    return make_admin(
        {'lan_tournament.administrate'}, screen_name='F03ConflictAdmin'
    )


@pytest.fixture(autouse=True)
def _context(admin_app):
    """Provide the app context, and leave no open transaction behind."""
    yield
    db.session.rollback()


def _viewer(user, *permissions: str) -> CurrentUser:
    return CurrentUser.create_authenticated(user, None, frozenset(permissions))


def _admin(admin) -> CurrentUser:
    return _viewer(admin, 'lan_tournament.administrate')


class Scene:
    """Builds committed tournaments of one party, with chosen people.

    A match is made of participants and teams the test names. A team
    without members demands nobody, so it stands in for the opponent
    when only one person is to be demanded.
    """

    def __init__(self, party_id: PartyID, confirmer) -> None:
        self.party_id = party_id
        self.confirmer = confirmer
        self._ghosts: dict[tuple, TournamentTeamID] = {}
        self._joined: dict[tuple, TournamentParticipantID] = {}
        self._orders = count(1)

    def tournament(
        self,
        *,
        name: str | None = None,
        status: TournamentStatus = ONGOING,
        game_format: GameFormat = ONE_V_ONE,
        mode: EliminationMode = SINGLE,
    ) -> TournamentID:
        tournament_id = TournamentID(uuid7())
        running = status is ONGOING
        repo.create_tournament(
            Tournament(
                id=tournament_id,
                party_id=self.party_id,
                name=name or f'Conflict {tournament_id}',
                game=None,
                description=None,
                image_url=None,
                ruleset=None,
                start_time=None,
                created_at=NOW,
                min_players=None,
                max_players=None,
                min_teams=None,
                max_teams=None,
                min_players_in_team=None,
                max_players_in_team=None,
                contestant_type=None,
                tournament_status=status,
                game_format=game_format,
                elimination_mode=mode,
                group_size_max=None,
                operational_clock_elapsed_us=(
                    0 if running else CLOCK_AT_NOW_US
                ),
                operational_clock_running_since=(
                    CLOCK_START if running else None
                ),
                operational_clock_activated_at=CLOCK_START,
            )
        )
        db.session.commit()
        return tournament_id

    def assign(self, tournament_id: TournamentID, user) -> None:
        db.session.add(
            DbTournamentOrga(
                TournamentOrgaID(uuid7()), tournament_id, user.id, NOW
            )
        )
        db.session.commit()

    def join(
        self,
        tournament_id: TournamentID,
        user,
        *,
        team_id: TournamentTeamID | None = None,
        removed: bool = False,
    ) -> TournamentParticipantID:
        """Register the user, once per tournament, and return the participant."""
        key = (tournament_id, user.id)
        if key in self._joined:
            return self._joined[key]

        participant = DbTournamentParticipant(
            TournamentParticipantID(uuid7()),
            user.id,
            tournament_id,
            NOW,
            team_id=team_id,
        )
        if removed:
            participant.removed_at = NOW - timedelta(minutes=5)
        db.session.add(participant)
        db.session.commit()
        self._joined[key] = participant.id
        return participant.id

    def team(
        self,
        tournament_id: TournamentID,
        name: str,
        *,
        captain,
        removed: bool = False,
    ) -> TournamentTeamID:
        team = DbTournamentTeam(
            TournamentTeamID(uuid7()), tournament_id, name, captain.id, NOW
        )
        if removed:
            team.removed_at = NOW - timedelta(minutes=5)
        db.session.add(team)
        db.session.commit()
        return team.id

    def ghost(self, tournament_id: TournamentID, number: int = 0):
        """Return a team of the tournament that has no member."""
        key = (tournament_id, number)
        if key not in self._ghosts:
            team = DbTournamentTeam(
                TournamentTeamID(uuid7()),
                tournament_id,
                f'Ghost {number} {uuid4().hex[:8]}',
                self.confirmer.id,
                NOW,
            )
            db.session.add(team)
            db.session.commit()
            self._ghosts[key] = team.id
        return self._ghosts[key]

    def match(
        self,
        tournament_id: TournamentID,
        *,
        participants=(),
        teams=(),
        round: int | None = 1,
        match_order: int | None = None,
        confirmed: bool = False,
        occupied: bool = False,
        wait_minutes: float | None = 10,
    ) -> TournamentMatchID:
        """Create a match; with `wait_minutes` it has an open episode."""
        match_id = TournamentMatchID(uuid7())
        db.session.add(
            DbTournamentMatch(
                match_id,
                tournament_id,
                NOW - timedelta(hours=1),
                match_order=(
                    next(self._orders) if match_order is None else match_order
                ),
                round=round,
                confirmed_by=self.confirmer.id if confirmed else None,
                phase=1,
                occupied_since=NOW - timedelta(minutes=30)
                if occupied
                else None,
            )
        )
        db.session.flush()
        entries = [{'participant_id': p} for p in participants]
        entries += [{'team_id': t} for t in teams]
        for kwargs in entries:
            db.session.add(
                DbTournamentMatchToContestant(
                    TournamentMatchToContestantID(uuid7()),
                    match_id,
                    NOW - timedelta(hours=1),
                    **kwargs,
                )
            )
        db.session.flush()
        if wait_minutes is not None:
            repo.open_due_episode_flush(
                MatchDueEpisode(
                    id=MatchDueEpisodeID(uuid7()),
                    tournament_id=tournament_id,
                    match_id=match_id,
                    pairing_key='key',
                    opened_at=NOW - timedelta(minutes=wait_minutes),
                    opened_clock_us=CLOCK_AT_NOW_US
                    - int(wait_minutes * MINUTE_US),
                )
            )
        db.session.commit()
        return match_id

    def solo(self, tournament_id, user, **fields) -> TournamentMatchID:
        """Create a match of one person against a team without members."""
        return self.match(
            tournament_id,
            participants=[self.join(tournament_id, user)],
            teams=[self.ghost(tournament_id)],
            **fields,
        )


@pytest.fixture
def make_scene(make_party, brand, confirmer):
    """Provide a factory of scenes, each in a party of its own."""

    def _make() -> Scene:
        party_id = PartyID(f'f03c-{uuid4().hex[:12]}')
        make_party(brand, party_id, f'F03 conflicts {party_id}')
        return Scene(party_id, confirmer)

    return _make


def _page(viewer, party_id, *, now=NOW, **fields):
    fields.setdefault('per_page', 50)
    result = service.get_dashboard_page(
        viewer,
        party_id,
        DashboardQuery(**fields),
        settings=SETTINGS,
        now=now,
    )
    return result.unwrap()


def _listing(viewer, party_id, **fields):
    """Read every page of a listing, in order."""
    fields.setdefault('view', 'all')
    fields.setdefault('per_page', 100)
    rows = []
    number = 1
    while True:
        page = _page(viewer, party_id, page=number, **fields)
        rows += page.rows
        if number >= page.total_pages:
            return rows
        number += 1


def _by_id(rows) -> dict:
    return {row.match_id: row for row in rows}


def _ref_ids(conflict: DashboardConflict) -> list:
    return [ref.match_id for ref in conflict.visible_refs]


# -- demand beyond the page and the assignments --


def test_off_page_same_party_demand_is_detected(make_scene, users, orga, admin):
    scene = make_scene()
    elsewhere = make_scene()
    alpha = scene.tournament(name='Alpha')
    bravo = scene.tournament(name='Bravo')
    scene.assign(alpha, orga)
    x, p1, y, p2, p3, p4, p5, p6 = users[:8]

    # x is needed in two matches of Alpha, y in one of Alpha and in one
    # of Bravo, which the orga is not assigned to. Nobody else overlaps.
    a1 = scene.match(
        alpha, participants=[scene.join(alpha, x), scene.join(alpha, p1)]
    )
    a2 = scene.match(
        alpha, participants=[scene.join(alpha, y), scene.join(alpha, p2)]
    )
    a3 = scene.match(
        alpha, participants=[scene.join(alpha, p3), scene.join(alpha, p4)]
    )
    a4 = scene.match(
        alpha, participants=[scene.join(alpha, x), scene.join(alpha, p5)]
    )
    b1 = scene.match(
        bravo, participants=[scene.join(bravo, y), scene.join(bravo, p6)]
    )
    # The same person is needed in another party: no demand of this one.
    other = elsewhere.tournament(name='Elsewhere')
    elsewhere.match(
        other,
        participants=[elsewhere.join(other, p3), elsewhere.join(other, p1)],
    )

    # The whole party, two matches a page: the partner of a1 is on page
    # two and the partner of a2 on page three, and both are found.
    admin_viewer = _admin(admin)
    first = _page(
        admin_viewer,
        scene.party_id,
        scope='all',
        sort='tournament',
        per_page=2,
    )
    assert [row.match_id for row in first.rows] == [a1, a2]
    assert first.total_pages == 3
    by_id = _by_id(first.rows)

    (x_conflict,) = by_id[a1].conflicts
    assert x_conflict.user_id == x.id
    assert x_conflict.user_display_name == x.screen_name
    assert _ref_ids(x_conflict) == [a4]
    assert x_conflict.visible_refs[0].list_page == 2
    assert x_conflict.visible_refs[0].tournament_name == 'Alpha'
    assert not x_conflict.has_external_conflict

    (y_conflict,) = by_id[a2].conflicts
    assert _ref_ids(y_conflict) == [b1]
    assert y_conflict.visible_refs[0].list_page == 3
    assert y_conflict.visible_refs[0].tournament_name == 'Bravo'
    assert not y_conflict.has_external_conflict

    everything = _by_id(
        _listing(admin_viewer, scene.party_id, scope='all', view='due')
    )
    assert everything[a3].conflicts == ()
    assert everything[a4].conflicts[0].user_id == x.id
    assert _ref_ids(everything[a4].conflicts[0]) == [a1]
    assert everything[b1].conflicts[0].user_id == y.id

    # The orga sees Alpha only. y's demand in Bravo is the one boolean.
    orga_viewer = _viewer(orga)
    mine = _page(orga_viewer, scene.party_id, sort='tournament', per_page=2)
    assert mine.total_count == 4
    by_id = _by_id(mine.rows)
    (x_conflict,) = by_id[a1].conflicts
    assert _ref_ids(x_conflict) == [a4]
    assert not x_conflict.has_external_conflict
    (y_conflict,) = by_id[a2].conflicts
    assert y_conflict.visible_refs == ()
    assert y_conflict.has_external_conflict
    assert str(b1) not in repr(mine)
    assert 'Bravo' not in repr(mine)

    # a4 is on the second page and finds its partner on the first.
    second = _page(
        orga_viewer, scene.party_id, sort='tournament', per_page=2, page=2
    )
    assert [row.match_id for row in second.rows] == [a3, a4]
    assert second.rows[0].conflicts == ()
    (back,) = second.rows[1].conflicts
    assert _ref_ids(back) == [a1]
    assert back.visible_refs[0].list_page == 1

    # The state filter sees the same conflicts, before the page is cut.
    conflicting = _page(
        orga_viewer, scene.party_id, state='conflict', sort='tournament'
    )
    assert conflicting.total_count == 3
    assert [row.match_id for row in conflicting.rows] == [a1, a2, a4]


# -- one person, however they are demanded --


def test_solo_and_cross_team_users_share_identity(make_scene, users, admin):
    scene = make_scene()
    solo_cup = scene.tournament(name='Solo-Cup')
    red_cup = scene.tournament(name='Rot-Cup')
    blue_cup = scene.tournament(name='Blau-Cup')
    u, v, w, g1, g2, opponent = users[:6]

    red = scene.team(red_cup, 'Rote Teufel', captain=v)
    green = scene.team(red_cup, 'Gruene Ameisen', captain=g1)
    blue = scene.team(blue_cup, 'Blaue Pelikane', captain=w)
    gold = scene.team(blue_cup, 'Goldene Adler', captain=g2)
    for team_id, tournament_id, members in (
        (red, red_cup, (u, v)),
        (green, red_cup, (g1,)),
        (blue, blue_cup, (u, w)),
        (gold, blue_cup, (g2,)),
    ):
        for member in members:
            scene.join(tournament_id, member, team_id=team_id)

    solo_match = scene.match(
        solo_cup,
        participants=[
            scene.join(solo_cup, u),
            scene.join(solo_cup, opponent),
        ],
    )
    red_match = scene.match(red_cup, teams=[red, green])
    blue_match = scene.match(blue_cup, teams=[blue, gold])
    # u is a different participant in each tournament.
    assert (
        len(
            {
                scene.join(solo_cup, u),
                scene.join(red_cup, u),
                scene.join(blue_cup, u),
            }
        )
        == 3
    )

    rows = _by_id(
        _listing(_admin(admin), scene.party_id, scope='all', view='due')
    )

    # Only u is needed twice or more: no other member of a team, and
    # nobody in one match only, is a conflict.
    for match_id in (solo_match, red_match, blue_match):
        assert len(rows[match_id].conflicts) == 1
        assert rows[match_id].conflicts[0].user_id == u.id
        assert rows[match_id].conflicts[0].user_display_name == u.screen_name

    # As a solo player, u is demanded by both teams, each named.
    (as_solo,) = rows[solo_match].conflicts
    assert as_solo.via_team_name is None
    assert [
        (ref.match_id, ref.via_team_name) for ref in as_solo.visible_refs
    ] == [(blue_match, 'Blaue Pelikane'), (red_match, 'Rote Teufel')]
    assert [ref.contestant_names for ref in as_solo.visible_refs] == [
        ('Blaue Pelikane', 'Goldene Adler'),
        ('Rote Teufel', 'Gruene Ameisen'),
    ]

    # As a member of one team, u meets the solo match and the other team.
    (as_red,) = rows[red_match].conflicts
    assert as_red.via_team_name == 'Rote Teufel'
    assert [
        (ref.match_id, ref.via_team_name) for ref in as_red.visible_refs
    ] == [(blue_match, 'Blaue Pelikane'), (solo_match, None)]
    assert as_red.visible_refs[1].contestant_names == (
        u.screen_name,
        opponent.screen_name,
    )

    (as_blue,) = rows[blue_match].conflicts
    assert as_blue.via_team_name == 'Blaue Pelikane'
    assert [
        (ref.match_id, ref.via_team_name) for ref in as_blue.visible_refs
    ] == [(red_match, 'Rote Teufel'), (solo_match, None)]
    assert not any(
        c.has_external_conflict for row in rows.values() for c in row.conflicts
    )


# -- what is no demand --


def test_a_counterpart_carries_the_format_of_its_own_phase(
    make_scene, users, admin
):
    scene = make_scene()
    cup = scene.tournament(name='Duell-Cup')
    lobbies = scene.tournament(
        name='Lobby-Cup', game_format=FREE_FOR_ALL, mode=SINGLE
    )
    u, a, b, c = users[:4]

    pairing = scene.solo(cup, u)
    lobby = scene.match(
        lobbies,
        participants=[scene.join(lobbies, p) for p in (u, a, b, c)],
        occupied=True,
        round=0,
    )

    rows = _by_id(
        _listing(_admin(admin), scene.party_id, scope='all', view='due')
    )

    # Each side names the other as what it is: a lobby or a pairing.
    (from_pairing,) = rows[pairing].conflicts
    (ref,) = from_pairing.visible_refs
    assert ref.match_id == lobby
    assert ref.game_format is FREE_FOR_ALL
    assert len(ref.contestant_names) == 4

    (from_lobby,) = rows[lobby].conflicts
    (ref,) = from_lobby.visible_refs
    assert ref.match_id == pairing
    assert ref.game_format is ONE_V_ONE
    assert rows[lobby].game_format is FREE_FOR_ALL


def test_future_paused_removed_and_registration_only_overlap_is_not_conflict(
    make_scene, users, admin
):
    scene = make_scene()
    anchors = scene.tournament(name='Anchors')
    anchor_orders = count(1)

    def anchor(user):
        """Make `user` needed in a due match of a running tournament."""
        return scene.solo(anchors, user, match_order=next(anchor_orders))

    # The control: the same shape, with two due matches, is a conflict.
    control, future, paused, gone, member, dead = users[:6]
    registered, done, completed, partial, lobby = users[6:11]
    control_tournament = scene.tournament(name='Control')
    control_match = scene.solo(control_tournament, control)
    anchored = {'control': anchor(control)}

    # A later round of a round robin is not due while an earlier one is.
    robin = scene.tournament(name='Robin', mode=ROUND_ROBIN)
    first_round = scene.match(
        robin,
        teams=[scene.ghost(robin, 0), scene.ghost(robin, 1)],
        round=1,
    )
    next_round = scene.match(
        robin,
        participants=[scene.join(robin, future)],
        teams=[scene.ghost(robin, 0)],
        round=2,
    )
    anchored['future'] = anchor(future)

    # A paused tournament has due matches, frozen: nobody is needed.
    frozen = scene.tournament(name='Frozen', status=PAUSED)
    paused_match = scene.solo(frozen, paused)
    anchored['paused'] = anchor(paused)

    # A participant who was removed is no longer needed in the match.
    left = scene.tournament(name='Left')
    removed_match = scene.match(
        left,
        participants=[scene.join(left, gone, removed=True)],
        teams=[scene.ghost(left)],
    )
    anchored['removed participant'] = anchor(gone)

    # Nor is a member who was removed from a team, ...
    squad = scene.tournament(name='Squad')
    squad_team = scene.team(squad, 'Schwund', captain=member)
    scene.join(squad, member, team_id=squad_team, removed=True)
    removed_member_match = scene.match(
        squad, teams=[squad_team, scene.ghost(squad)]
    )
    anchored['removed member'] = anchor(member)

    # ... nor a member of a team that was removed.
    ruins = scene.tournament(name='Ruins')
    dead_team = scene.team(ruins, 'Verschwunden', captain=dead, removed=True)
    scene.join(ruins, dead, team_id=dead_team)
    removed_team_match = scene.match(
        ruins, teams=[dead_team, scene.ghost(ruins)]
    )
    anchored['removed team'] = anchor(dead)

    # A registration is no demand, and a bracket before the start is none.
    signup = scene.tournament(
        name='Signup', status=TournamentStatus.REGISTRATION_OPEN
    )
    scene.join(signup, registered)
    closed = scene.tournament(
        name='Closed', status=TournamentStatus.REGISTRATION_CLOSED
    )
    prestart_match = scene.solo(closed, registered)
    anchored['registered'] = anchor(registered)

    # A confirmed match and a finished tournament demand nobody.
    settled = scene.tournament(name='Settled')
    confirmed_match = scene.solo(
        settled, done, confirmed=True, wait_minutes=None
    )
    anchored['confirmed'] = anchor(done)
    finished = scene.tournament(
        name='Finished', status=TournamentStatus.COMPLETED
    )
    finished_match = scene.solo(finished, completed)
    anchored['completed'] = anchor(completed)

    # A match with a single contestant and a lobby that is not complete
    # are coming demand, not current demand.
    thin = scene.tournament(name='Thin')
    partial_match = scene.match(
        thin, participants=[scene.join(thin, partial)], wait_minutes=None
    )
    anchored['partial'] = anchor(partial)
    arena = scene.tournament(name='Arena', game_format=FREE_FOR_ALL)
    lobby_match = scene.match(
        arena,
        participants=[scene.join(arena, lobby)],
        teams=[scene.ghost(arena, 0), scene.ghost(arena, 1)],
        occupied=False,
        wait_minutes=None,
    )
    anchored['lobby'] = anchor(lobby)

    viewer = _admin(admin)
    rows = _by_id(_listing(viewer, scene.party_id, scope='all'))

    # Every case is what it claims to be.
    assert rows[control_match].state is DashboardRowState.DUE
    assert rows[next_round].state is DashboardRowState.UPCOMING
    assert rows[first_round].state is DashboardRowState.DUE
    assert rows[paused_match].state is DashboardRowState.PAUSED
    assert rows[removed_match].state is DashboardRowState.DUE
    assert rows[removed_member_match].state is DashboardRowState.DUE
    assert rows[removed_team_match].state is DashboardRowState.DUE
    assert rows[prestart_match].state is DashboardRowState.UPCOMING
    assert rows[confirmed_match].state is DashboardRowState.DONE
    assert rows[finished_match].state is DashboardRowState.DONE
    assert rows[partial_match].state is DashboardRowState.PARTIAL
    assert rows[lobby_match].state is DashboardRowState.AWAITING_LOBBY
    assert all(rows[match_id].state is DUE for match_id in anchored.values())

    # Only the control is in conflict; no other row has even a trace.
    in_conflict = {m for m, row in rows.items() if row.conflicts}
    assert in_conflict == {control_match, anchored['control']}
    (conflict,) = rows[anchored['control']].conflicts
    assert conflict.user_id == control.id
    assert _ref_ids(conflict) == [control_match]
    assert not any(
        c.has_external_conflict for r in rows.values() for c in r.conflicts
    )

    # The filter agrees, in every list that holds the rows.
    for view in ('due', 'all'):
        page = _page(
            viewer, scene.party_id, scope='all', view=view, state='conflict'
        )
        assert {row.match_id for row in page.rows} == in_conflict
        assert page.total_count == 2


# -- what leaves the database --


def test_external_projection_exposes_boolean_only(
    make_scene, users, orga, admin
):
    x, y, hidden_only = users[:3]
    seen = {}

    for hidden in (1, 2, 5):
        scene = make_scene()
        alpha = scene.tournament(name='Alpha')
        secret = scene.tournament(name=f'Geheim-{hidden}-Turnier')
        scene.assign(alpha, orga)
        visible = scene.solo(alpha, x)
        plain = scene.solo(alpha, y)
        # The same person is needed in `hidden` matches the orga may not
        # see, some of them as a member of a team with a name of its own.
        squad = scene.team(secret, f'Geheimstaffel {hidden}', captain=x)
        scene.join(secret, x, team_id=squad)
        hidden_ids = []
        for number in range(hidden):
            if number % 2:
                hidden_ids.append(
                    scene.match(
                        secret, teams=[squad, scene.ghost(secret, number)]
                    )
                )
            else:
                hidden_ids.append(
                    scene.match(
                        secret,
                        participants=[scene.join(secret, x)],
                        teams=[scene.ghost(secret, number)],
                    )
                )
        # Somebody who is needed twice, but only in matches of the orga's
        # blind spot, is nothing the orga learns about.
        for number in (10, 11):
            hidden_ids.append(
                scene.match(
                    secret,
                    participants=[scene.join(secret, hidden_only)],
                    teams=[scene.ghost(secret, number)],
                )
            )

        viewer = _viewer(orga)
        page = _page(viewer, scene.party_id)
        rows = _by_id(page.rows)
        assert set(rows) == {visible, plain}
        (conflict,) = rows[visible].conflicts
        assert conflict == DashboardConflict(
            user_id=x.id,
            user_display_name=x.screen_name,
            via_team_name=None,
            visible_refs=(),
            has_external_conflict=True,
        )
        assert rows[plain].conflicts == ()
        assert page.total_count == 2

        # Neither the page nor the repository's answer names a hidden thing.
        secrets = [
            str(secret),
            f'Geheim-{hidden}-Turnier',
            f'Geheimstaffel {hidden}',
            hidden_only.screen_name,
            *map(str, hidden_ids),
        ]
        scope = service.resolve_dashboard_scope(
            viewer, scene.party_id, 'assigned'
        ).unwrap()
        with repository.read_snapshot():
            data = repository.query_dashboard_matches(
                scope, DashboardQuery(per_page=50), now=NOW, settings=SETTINGS
            )
        for text in (repr(page), repr(data)):
            for hidden_text in secrets:
                assert hidden_text not in text, hidden_text
        assert set(data.contestants) == {visible, plain}
        (record,) = data.conflicts[visible]
        assert record.refs == ()
        assert record.has_external_conflict
        assert record.via_team_name is None
        assert plain not in data.conflicts

        # The administrator sees the same demand as references.
        everything = _by_id(
            _listing(_admin(admin), scene.party_id, scope='all', view='due')
        )
        seen_by_admin = everything[visible].conflicts[0]
        assert len(seen_by_admin.visible_refs) == hidden
        assert not seen_by_admin.has_external_conflict

        seen[hidden] = (
            [r.conflicts for r in page.rows],
            page.total_count,
            page.tier_counts,
            page.non_actionable_counts,
            [(r.state, r.tier) for r in page.rows],
        )

    # One hidden match, two and five: the same answer in every respect.
    assert seen[1] == seen[2] == seen[5]
    assert repr(seen[1]) == repr(seen[5])


# -- the position in the list --


def test_counterpart_list_page_follows_filtered_sorted_result(
    make_scene, users, admin
):
    scene = make_scene()
    alpha = scene.tournament(name='Alpha')
    bravo = scene.tournament(name='Bravo')
    x, y = users[:2]

    # Two people are needed twice; the rest only gives the list a shape.
    ids = {
        'x_alpha': scene.solo(alpha, x, wait_minutes=5, match_order=1),
        'x_bravo': scene.solo(bravo, x, wait_minutes=50, match_order=1),
        'y_early': scene.solo(alpha, y, wait_minutes=35, match_order=20),
        'y_late': scene.solo(alpha, y, wait_minutes=1, match_order=21),
    }
    for number, wait in enumerate([60, 40, 30, 20, 10, 8, 3], start=2):
        scene.match(
            alpha,
            teams=[scene.ghost(alpha, 0), scene.ghost(alpha, 1)],
            wait_minutes=wait,
            match_order=number,
        )
    viewer = _admin(admin)
    conflicted = set(ids.values())

    queries = [
        {},
        {'state': 'conflict'},
        {'state': 'tier-red'},
        {'state': 'tier-green'},
        {'tournament_id': alpha},
        {'tournament_id': bravo},
    ]
    positions = set()
    stale = 0
    for sort in ('urgency', 'wait', 'tournament'):
        for query in queries:
            full = [
                row.match_id
                for row in _listing(
                    viewer,
                    scene.party_id,
                    scope='all',
                    view='due',
                    sort=sort,
                    **query,
                )
            ]
            for per_page in (2, 3):
                number = 0
                while True:
                    number += 1
                    page = _page(
                        viewer,
                        scene.party_id,
                        scope='all',
                        view='due',
                        sort=sort,
                        per_page=per_page,
                        page=number,
                        **query,
                    )
                    for row in page.rows:
                        # Whatever is in conflict has a conflict, anywhere
                        # in the list, and nothing else has one.
                        assert bool(row.conflicts) == (
                            row.match_id in conflicted
                        ), (sort, query, per_page, number)
                        for conflict in row.conflicts:
                            for ref in conflict.visible_refs:
                                expected = (
                                    full.index(ref.match_id) // per_page + 1
                                    if ref.match_id in full
                                    else None
                                )
                                assert ref.list_page == expected, (
                                    sort,
                                    query,
                                    per_page,
                                    number,
                                    ref.tournament_name,
                                )
                                positions.add(ref.list_page)
                                stale += ref.list_page is None
                                if ref.list_page not in (None, number):
                                    positions.add('elsewhere')
                    if number >= page.total_pages:
                        break

    # The check is no vacuous one: the pages differ and a filter drops some.
    assert stale > 0
    assert 'elsewhere' in positions
    assert {1, 2, 3} <= positions


# -- naming the team --


def test_conflict_role_names_only_authorized_team(
    make_scene, users, orga, admin
):
    scene = make_scene()
    first = scene.tournament(name='Alpha-Liga')
    second = scene.tournament(name='Beta-Liga')
    third = scene.tournament(name='Gamma-Liga')
    scene.assign(first, orga)
    scene.assign(third, orga)
    u = users[0]

    alpha_squad = scene.team(first, 'Alpha Staffel', captain=u)
    beta_squad = scene.team(second, 'Beta Staffel', captain=u)
    scene.join(first, u, team_id=alpha_squad)
    scene.join(second, u, team_id=beta_squad)
    m_first = scene.match(first, teams=[alpha_squad, scene.ghost(first)])
    m_second = scene.match(second, teams=[beta_squad, scene.ghost(second)])
    m_third = scene.solo(third, u)

    # The orga is assigned to the first and the third tournament.
    page = _page(_viewer(orga), scene.party_id)
    rows = _by_id(page.rows)
    assert set(rows) == {m_first, m_third}

    (in_first,) = rows[m_first].conflicts
    assert in_first.via_team_name == 'Alpha Staffel'
    assert [(r.match_id, r.via_team_name) for r in in_first.visible_refs] == [
        (m_third, None)
    ]
    assert in_first.has_external_conflict

    (in_third,) = rows[m_third].conflicts
    assert in_third.via_team_name is None
    assert [(r.match_id, r.via_team_name) for r in in_third.visible_refs] == [
        (m_first, 'Alpha Staffel')
    ]
    assert in_third.has_external_conflict
    # The team of the match in the blind spot is named nowhere.
    assert 'Beta' not in repr(page)

    # The administrator may see all three, each with the team of its own.
    everything = _by_id(
        _listing(_admin(admin), scene.party_id, scope='all', view='due')
    )
    (in_second,) = everything[m_second].conflicts
    assert in_second.via_team_name == 'Beta Staffel'
    assert [(r.match_id, r.via_team_name) for r in in_second.visible_refs] == [
        (m_first, 'Alpha Staffel'),
        (m_third, None),
    ]
    assert not in_second.has_external_conflict
    (in_first,) = everything[m_first].conflicts
    assert [(r.match_id, r.via_team_name) for r in in_first.visible_refs] == [
        (m_second, 'Beta Staffel'),
        (m_third, None),
    ]
    assert not in_first.has_external_conflict


# -- the filter and the weight --


@pytest.mark.parametrize('hidden', [1, 4])
def test_conflict_filter_and_urgency_weight_precede_pagination(
    make_scene, users, orga, admin, hidden
):
    scene = make_scene()
    alpha = scene.tournament(name='Alpha')
    secret = scene.tournament(name='Geheim')
    scene.assign(alpha, orga)
    a, b, c, x, d = users[:5]

    red = scene.solo(alpha, a, wait_minutes=60)
    yellow = scene.solo(alpha, b, wait_minutes=20)
    # Two matches that need the same person, one match that needs one
    # who is needed elsewhere too, and a plain one that has waited longer.
    conflict_two = scene.solo(alpha, c, wait_minutes=4)
    conflict_one = scene.match(
        alpha,
        participants=[scene.join(alpha, c)],
        teams=[scene.ghost(alpha, 1)],
        wait_minutes=2,
    )
    external = scene.solo(alpha, x, wait_minutes=1)
    plain_long = scene.solo(alpha, d, wait_minutes=12)
    hidden_ids = [
        scene.match(
            secret,
            participants=[scene.join(secret, x)],
            teams=[scene.ghost(secret, number)],
            wait_minutes=5,
        )
        for number in range(hidden)
    ]

    # The conflict is weighed after the tier and before the interval: the
    # plain match that has waited 12 minutes follows all three that are in
    # conflict, whose intervals are shorter. The hidden matches may be one
    # or many, the order is the same.
    viewer = _viewer(orga)
    expected = [
        red,
        yellow,
        conflict_two,
        conflict_one,
        external,
        plain_long,
    ]
    assert [
        r.match_id for r in _listing(viewer, scene.party_id, view='due')
    ] == expected
    for per_page, pages in ((2, [expected[0:2], expected[2:4], expected[4:]]),):
        for number, ids in enumerate(pages, start=1):
            page = _page(viewer, scene.party_id, per_page=per_page, page=number)
            assert [r.match_id for r in page.rows] == ids
            assert page.total_count == 6

    # The filter is applied before the cut, and the total follows it.
    conflicting = [conflict_two, conflict_one, external]
    first = _page(viewer, scene.party_id, state='conflict', per_page=2)
    second = _page(viewer, scene.party_id, state='conflict', per_page=2, page=2)
    beyond = _page(viewer, scene.party_id, state='conflict', per_page=2, page=3)
    assert [r.match_id for r in first.rows] == conflicting[:2]
    assert [r.match_id for r in second.rows] == conflicting[2:]
    assert (first.total_count, first.total_pages) == (3, 2)
    assert beyond.rows == () and beyond.total_count == 3
    assert beyond.page == 3

    # The tiers are counted by the interval alone.
    assert (first.tier_counts.red, first.tier_counts.yellow) == (1, 1)
    assert first.tier_counts.green == 4

    # The external hint weighs as much as an authorized conflict.
    assert second.rows[0].match_id == external
    (hint,) = second.rows[0].conflicts
    assert hint.has_external_conflict and hint.visible_refs == ()

    # The wait order and the tournament order know no conflict.
    by_wait = [
        r.match_id
        for r in _listing(viewer, scene.party_id, view='due', sort='wait')
    ]
    assert by_wait == [
        red,
        yellow,
        plain_long,
        conflict_two,
        conflict_one,
        external,
    ]

    # The administrator sees the hidden matches too, and all of them count.
    everything = _page(
        _admin(admin),
        scene.party_id,
        scope='all',
        state='conflict',
        per_page=100,
    )
    assert {r.match_id for r in everything.rows} == {
        *conflicting,
        *hidden_ids,
    }
    assert everything.total_count == 3 + hidden


# -- the statements --


@contextmanager
def _recorded_statements():
    statements: list[str] = []

    def on_execute(connection, cursor, statement, *args):
        statements.append(statement)

    event.listen(db.engine, 'before_cursor_execute', on_execute)
    try:
        yield statements
    finally:
        event.remove(db.engine, 'before_cursor_execute', on_execute)


def _read(viewer, party_id, per_page):
    with _recorded_statements() as statements:
        # No `now`: the snapshot reads the server clock itself.
        result = service.get_dashboard_page(
            viewer,
            party_id,
            DashboardQuery(scope='all', per_page=per_page),
            settings=SETTINGS,
        )
    return result.unwrap(), statements


def test_conflicts_add_one_bounded_read_only_statement(
    make_scene, users, admin
):
    viewer = _admin(admin)

    crowded = make_scene()
    tournament = crowded.tournament(name='Crowded')
    for order in range(40):
        crowded.solo(tournament, users[0], wait_minutes=1 + order)
    calm = make_scene()
    quiet = calm.tournament(name='Quiet')
    # One person is listed, so the names are read as they are for the
    # crowd, but nobody is needed twice.
    calm.solo(quiet, users[1], wait_minutes=100)
    for order in range(39):
        calm.match(
            quiet,
            teams=[calm.ghost(quiet, 0), calm.ghost(quiet, 1)],
            wait_minutes=1 + order,
        )

    counts = {}
    for name, scene in (('crowded', crowded), ('calm', calm)):
        for per_page in (5, 40):
            page, statements = _read(viewer, scene.party_id, per_page)
            assert len(page.rows) == per_page
            counts[name, per_page] = len(statements)
            for statement in statements:
                head = statement.lstrip().split(None, 1)[0].upper()
                assert head in {'SELECT', 'WITH'}, statement
                assert 'FOR UPDATE' not in statement.upper()
                assert 'FOR SHARE' not in statement.upper()
            assert all(
                bool(row.conflicts) == (name == 'crowded') for row in page.rows
            )

    # Growth of the page adds no statement, and the budget holds. The
    # conflicts are read by the statement of the page, so a page with
    # conflicts reads no statement more than one without (the name dates
    # from when they had a statement of their own).
    assert counts['crowded', 5] == counts['crowded', 40]
    assert counts['calm', 5] == counts['calm', 40]
    assert counts['crowded', 5] == counts['calm', 5]
    assert counts['crowded', 5] <= 12
