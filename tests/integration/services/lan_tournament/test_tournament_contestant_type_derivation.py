"""
tests.integration.services.lan_tournament.test_tournament_contestant_type_derivation
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Covers the `contestant_type` derivation added for bead workspace-pv3b.14:
a missing `contestant_type` is derived from team size ("bigger than 1
is TEAM, otherwise SOLO") at `create_tournament` and `update_tournament`,
so the stored value is never NULL for a tournament these functions touch.

The legacy-row section (workspace-pv3b.21, G3) covers a row that
predates this fix and is already NULL in the database. Superseding
the pv3b.14 "derive only at the three guard sites, never persist"
decision, the row-to-dataclass mapping in `tournament_repository`
(`_db_tournament_to_tournament`) now derives `contestant_type` once,
so every reader -- guards, services, view helpers -- sees TEAM/SOLO
without repeating the derivation and without ever writing it back to
the row.

The `change_status` section (workspace-pv3b.24, H2) covers the write
path fix 2 missed: `change_status` used to `replace()` the loaded
(now derived) dataclass and save it through the full-row
`update_tournament`, so every status change on a legacy NULL row
wrote TEAM/SOLO into the column. Fix 3 makes it write only the status
column (`set_tournament_status_flush`), so a legacy row stays NULL
across every transition. The legacy row is created FIRST here, then
transitioned -- fix 2's own test changed the status before nulling
the column and never actually exercised this path.
"""

import dataclasses
from unittest.mock import patch

import pytest
from sqlalchemy import select
from werkzeug.exceptions import NotFound

from byceps.database import db
from byceps.services.lan_tournament import (
    tournament_match_service,
    tournament_participant_service,
    tournament_repository,
    tournament_service,
    tournament_team_service,
)
from byceps.services.lan_tournament.blueprints.site.views import (
    _require_team_tournament,
)
from byceps.services.lan_tournament.dbmodels.tournament import DbTournament
from byceps.services.lan_tournament.lan_tournament_view_helpers import (
    _resolve_contestant_name,
    build_contestant_name_lookups,
)
from byceps.services.lan_tournament.models import (
    ContestantType,
    EliminationMode,
    GameFormat,
    TournamentStatus,
)
from byceps.services.party.models import PartyID
from byceps.services.ticketing import ticket_creation_service


PARTY_ID = PartyID('lan-party-contestant-type-derivation')


@pytest.fixture(scope='module')
def party(make_party, brand):
    return make_party(brand, PARTY_ID, 'Contestant Type Derivation Party')


@pytest.fixture(scope='module')
def ticket_category(make_ticket_category, party):
    return make_ticket_category(party.id, 'Contestant Type Entry')


@pytest.fixture(scope='module')
def grant_ticket(ticket_category):
    def _grant(user):
        return ticket_creation_service.create_ticket(
            ticket_category, user, user=user
        )

    return _grant


@pytest.fixture(scope='module')
def team_user1(make_user):
    return make_user('ContestantTypeUser1')


@pytest.fixture(scope='module')
def team_user2(make_user):
    return make_user('ContestantTypeUser2')


def _create_ok(*args, **kwargs):
    result = tournament_service.create_tournament(*args, **kwargs)
    assert result.is_ok(), result.unwrap_err()
    tournament, _event = result.unwrap()
    return tournament


def _raw_db_contestant_type(tournament_id) -> str | None:
    """Read `contestant_type` straight off the row, bypassing the
    dataclass mapping entirely -- a plain column select, never a
    full-entity load, so it always reflects what is actually stored."""
    return db.session.execute(
        select(DbTournament.contestant_type).where(
            DbTournament.id == tournament_id
        )
    ).scalar_one()


# -------------------------------------------------------------------- #
# create_tournament


def test_create_with_no_contestant_type_and_team_size_derives_team(party):
    tournament = _create_ok(
        PARTY_ID,
        'Derive Team Create',
        max_players_in_team=2,
    )

    assert tournament.contestant_type == ContestantType.TEAM


@pytest.mark.parametrize('max_players_in_team', [1, None])
def test_create_with_no_contestant_type_and_no_team_derives_solo(
    party, max_players_in_team
):
    tournament = _create_ok(
        PARTY_ID,
        f'Derive Solo Create {max_players_in_team}',
        max_players_in_team=max_players_in_team,
    )

    assert tournament.contestant_type == ContestantType.SOLO


def test_create_with_explicit_contestant_type_is_kept(party):
    tournament = _create_ok(
        PARTY_ID,
        'Explicit Type Kept Create',
        contestant_type=ContestantType.SOLO,
        max_players_in_team=5,
    )

    assert tournament.contestant_type == ContestantType.SOLO


# -------------------------------------------------------------------- #
# update_tournament


def test_update_with_no_contestant_type_and_team_size_derives_team(party):
    tournament = _create_ok(PARTY_ID, 'Derive Team Update')

    result = tournament_service.update_tournament(
        tournament.id,
        name=tournament.name,
        max_players_in_team=3,
    )

    assert result.is_ok(), result.unwrap_err()
    assert result.unwrap().contestant_type == ContestantType.TEAM


def test_update_locked_tournament_with_unchanged_derived_type_is_not_a_change(
    party,
):
    """A legacy NULL `contestant_type` whose team size derives to the
    same effective type as the incoming (also derived) one must not
    trip the ONGOING/PAUSED locked-change guard."""
    tournament = _create_ok(
        PARTY_ID,
        'Legacy Locked Unchanged Update',
        max_players_in_team=1,
    )
    assert tournament.contestant_type == ContestantType.SOLO

    # Simulate a legacy row: NULL contestant_type in the database,
    # written directly through the repository, bypassing the service.
    legacy = dataclasses.replace(
        tournament,
        contestant_type=None,
        tournament_status=TournamentStatus.ONGOING,
    )
    tournament_repository.update_tournament(legacy)
    tournament_repository.commit_session()

    result = tournament_service.update_tournament(
        tournament.id,
        name=tournament.name,
        max_players_in_team=1,
    )

    assert result.is_ok(), result.unwrap_err()
    assert result.unwrap().contestant_type == ContestantType.SOLO


# -------------------------------------------------------------------- #
# legacy NULL row: derived once at the repository read, never persisted


def _write_legacy_null_contestant_type(tournament):
    """Simulate a pre-fix row: NULL `contestant_type` in the database,
    written directly through the repository, bypassing the service."""
    legacy = dataclasses.replace(tournament, contestant_type=None)
    tournament_repository.update_tournament(legacy)
    tournament_repository.commit_session()


def test_legacy_null_row_with_team_size_is_derived_as_team_via_service(
    party,
):
    """G3: a legacy NULL row with `max_players_in_team=2`, loaded via
    `tournament_service.get_tournament`/`find_tournament`, comes back
    with `contestant_type == TEAM` -- derived once at the repository
    read, so every caller of the service wrapper sees it too."""
    tournament = _create_ok(
        PARTY_ID,
        'Legacy Null Team Via Service',
        contestant_type=ContestantType.SOLO,
        max_players_in_team=2,
    )
    _write_legacy_null_contestant_type(tournament)

    assert (
        tournament_service.get_tournament(tournament.id).contestant_type
        == ContestantType.TEAM
    )
    assert (
        tournament_service.find_tournament(tournament.id).contestant_type
        == ContestantType.TEAM
    )

    # The row itself was never rewritten by the derivation.
    assert _raw_db_contestant_type(tournament.id) is None


def test_legacy_null_row_with_no_team_size_is_derived_as_solo_via_service(
    party,
):
    """G3: a legacy NULL row with no team size derives to SOLO via the
    same service wrappers."""
    tournament = _create_ok(
        PARTY_ID,
        'Legacy Null Solo Via Service',
        contestant_type=ContestantType.SOLO,
    )
    _write_legacy_null_contestant_type(tournament)

    assert (
        tournament_service.get_tournament(tournament.id).contestant_type
        == ContestantType.SOLO
    )
    assert (
        tournament_service.find_tournament(tournament.id).contestant_type
        == ContestantType.SOLO
    )

    assert _raw_db_contestant_type(tournament.id) is None


def test_legacy_null_row_with_team_size_is_treated_as_team(party):
    """A legacy NULL `contestant_type` with team size > 1 is treated as
    TEAM: the site guard lets team routes through, and bracket
    preparation fetches teams, not participants -- all without writing
    the derived value back to the row."""
    tournament = _create_ok(
        PARTY_ID,
        'Legacy Null Team Size',
        contestant_type=ContestantType.SOLO,
        max_players_in_team=2,
    )
    _write_legacy_null_contestant_type(tournament)

    reloaded = tournament_repository.get_tournament(tournament.id)
    assert reloaded.contestant_type == ContestantType.TEAM  # derived, not NULL
    assert _raw_db_contestant_type(tournament.id) is None  # still NULL in DB

    _require_team_tournament(reloaded)  # must not abort

    with (
        patch.object(
            tournament_repository,
            'get_teams_for_tournament',
            wraps=tournament_repository.get_teams_for_tournament,
        ) as spy_get_teams,
        patch.object(
            tournament_repository, 'get_participants_for_tournament'
        ) as spy_get_participants,
    ):
        tournament_match_service.generate_single_elimination_bracket(
            tournament.id
        )

    spy_get_teams.assert_called_once_with(tournament.id)
    spy_get_participants.assert_not_called()

    # The row itself was never rewritten by the derivation.
    assert _raw_db_contestant_type(tournament.id) is None


def test_legacy_null_row_with_no_team_size_is_treated_as_solo(party):
    """A legacy NULL `contestant_type` with no team size is treated as
    SOLO: the site guard 404s team routes, and bracket preparation
    fetches participants, not teams -- all without writing the derived
    value back to the row."""
    tournament = _create_ok(
        PARTY_ID,
        'Legacy Null Solo Size',
        contestant_type=ContestantType.SOLO,
    )
    _write_legacy_null_contestant_type(tournament)

    reloaded = tournament_repository.get_tournament(tournament.id)
    assert reloaded.contestant_type == ContestantType.SOLO
    assert _raw_db_contestant_type(tournament.id) is None

    with pytest.raises(NotFound):
        _require_team_tournament(reloaded)

    with (
        patch.object(
            tournament_repository,
            'get_participants_for_tournament',
            wraps=tournament_repository.get_participants_for_tournament,
        ) as spy_get_participants,
        patch.object(
            tournament_repository, 'get_teams_for_tournament'
        ) as spy_get_teams,
    ):
        tournament_match_service.generate_single_elimination_bracket(
            tournament.id
        )

    spy_get_participants.assert_called_once_with(tournament.id)
    spy_get_teams.assert_not_called()

    assert _raw_db_contestant_type(tournament.id) is None


def test_legacy_null_row_with_two_teams_resolves_team_names(
    party, team_user1, team_user2, grant_ticket
):
    """G3: with two teams seeded into a bracket, a legacy NULL row
    (team size > 1) resolves contestant names to the team names --
    not "TBD" -- and the site guard treats it as a team tournament,
    exactly as an explicitly-typed TEAM tournament would."""
    tournament = _create_ok(
        PARTY_ID,
        'Legacy Null Two Teams',
        contestant_type=ContestantType.TEAM,
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        max_teams=4,
        min_players_in_team=1,
        max_players_in_team=2,
    )
    open_result = tournament_service.change_status(
        tournament.id, TournamentStatus.REGISTRATION_OPEN
    )
    assert open_result.is_ok()

    for user in (team_user1, team_user2):
        grant_ticket(user)
        join_result = tournament_participant_service.join_tournament(
            tournament.id, user.id
        )
        assert join_result.is_ok()

    team1_result = tournament_team_service.create_team(
        tournament.id, 'Legacy Team Alpha', team_user1.id
    )
    team2_result = tournament_team_service.create_team(
        tournament.id, 'Legacy Team Beta', team_user2.id
    )
    assert team1_result.is_ok()
    assert team2_result.is_ok()

    # Now downgrade the row to a legacy NULL `contestant_type`, as a
    # pre-fix row would be, and generate the bracket against that.
    _write_legacy_null_contestant_type(tournament)
    reloaded = tournament_repository.get_tournament(tournament.id)
    assert reloaded.contestant_type == ContestantType.TEAM
    assert _raw_db_contestant_type(tournament.id) is None

    _require_team_tournament(reloaded)  # site team section: not a 404

    generate_result = (
        tournament_match_service.generate_single_elimination_bracket(
            tournament.id
        )
    )
    assert generate_result.is_ok(), generate_result.unwrap_err()

    matches = tournament_match_service.get_matches_for_tournament(tournament.id)
    assert len(matches) == 1
    contestants = tournament_match_service.get_contestants_for_match(
        matches[0].id
    )
    assert len(contestants) == 2

    teams_by_id, participants_by_id = build_contestant_name_lookups(
        tournament.id, [contestants]
    )
    resolved_names = {
        _resolve_contestant_name(c, teams_by_id, participants_by_id)
        for c in contestants
    }
    assert resolved_names == {'Legacy Team Alpha', 'Legacy Team Beta'}


# -------------------------------------------------------------------- #
# change_status must not persist the derived type (workspace-pv3b.24, H2)


def test_legacy_null_team_row_status_changes_stay_null_in_the_column(
    party,
):
    """H2: create the legacy NULL row FIRST, then run it through
    `change_status` -- DRAFT -> REGISTRATION_OPEN ->
    REGISTRATION_CLOSED -> ONGOING (the start path). After every
    transition the raw column is still NULL, the status did change,
    and the loaded dataclass still derives TEAM."""
    tournament = _create_ok(
        PARTY_ID,
        'Legacy Null Team Status Changes',
        contestant_type=ContestantType.SOLO,
        tournament_status=TournamentStatus.DRAFT,
        max_players_in_team=2,
    )
    _write_legacy_null_contestant_type(tournament)
    assert _raw_db_contestant_type(tournament.id) is None

    for new_status in (
        TournamentStatus.REGISTRATION_OPEN,
        TournamentStatus.REGISTRATION_CLOSED,
        TournamentStatus.ONGOING,
    ):
        result = tournament_service.change_status(tournament.id, new_status)
        assert result.is_ok(), result.unwrap_err()
        updated, _event = result.unwrap()

        assert updated.tournament_status == new_status
        assert updated.contestant_type == ContestantType.TEAM
        assert _raw_db_contestant_type(tournament.id) is None


def test_legacy_null_solo_row_status_changes_stay_null_in_the_column(
    party,
):
    """H2: the same transitions on a legacy NULL row with no team size
    -- the column stays NULL and the dataclass keeps deriving SOLO."""
    tournament = _create_ok(
        PARTY_ID,
        'Legacy Null Solo Status Changes',
        contestant_type=ContestantType.SOLO,
        tournament_status=TournamentStatus.DRAFT,
    )
    _write_legacy_null_contestant_type(tournament)
    assert _raw_db_contestant_type(tournament.id) is None

    for new_status in (
        TournamentStatus.REGISTRATION_OPEN,
        TournamentStatus.REGISTRATION_CLOSED,
        TournamentStatus.ONGOING,
    ):
        result = tournament_service.change_status(tournament.id, new_status)
        assert result.is_ok(), result.unwrap_err()
        updated, _event = result.unwrap()

        assert updated.tournament_status == new_status
        assert updated.contestant_type == ContestantType.SOLO
        assert _raw_db_contestant_type(tournament.id) is None
