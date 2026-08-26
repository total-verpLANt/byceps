"""Current readiness authority, independent of Flask request/session state.

Mutation callers must hold the tournament lock and supply its freshly read
current pairing. This service validates contestant ownership and current roster
facts; generation/revision and lifecycle checks belong to the mutation service.
"""

from byceps.services.user.models import UserID
from byceps.util.authz import get_permissions_for_user
from byceps.util.result import Err, Ok, Result

from . import (
    tournament_orga_service,
    tournament_participant_service,
    tournament_team_service,
)
from .models.match_readiness import ContestantIdentity, MatchPairing
from .models.tournament import TournamentID
from .models.tournament_match import MatchSide
from .models.tournament_participant import TournamentParticipantID
from .models.tournament_team import TournamentTeamID


def authorize_readiness_side(
    tournament_id: TournamentID,
    pairing: MatchPairing,
    side: MatchSide,
    initiator_id: UserID,
) -> Result[str, str]:
    """Return actor role ``player``/``orga`` for a currently authorized side."""
    if not isinstance(side, MatchSide):
        return Err('invalid_match_side')
    result = _resolve_authority(tournament_id, pairing, initiator_id)
    if result.is_err():
        return Err(result.unwrap_err())
    role, sides = result.unwrap()
    if side not in sides:
        return Err('readiness_forbidden')
    return Ok(role)


def get_user_readiness_sides(
    tournament_id: TournamentID,
    pairing: MatchPairing,
    user_id: UserID,
) -> Result[frozenset[MatchSide], str]:
    """Resolve current capabilities without trusting a cached viewer role."""
    result = _resolve_authority(tournament_id, pairing, user_id)
    if result.is_err():
        return Err(result.unwrap_err())
    return Ok(result.unwrap()[1])


def _resolve_authority(
    tournament_id: TournamentID, pairing: MatchPairing, user_id: UserID
) -> Result[tuple[str, frozenset[MatchSide]], str]:
    if pairing.tournament_id != tournament_id:
        return Err('pairing_tournament_mismatch')
    if pairing.ended_at is not None or pairing.side_a == pairing.side_b:
        return Err('readiness_pairing_invalid')

    sides = set()
    for side, identity in (
        (MatchSide.A, pairing.side_a), (MatchSide.B, pairing.side_b)
    ):
        ownership = _owns_identity(tournament_id, identity, user_id)
        if ownership.is_err():
            return Err(ownership.unwrap_err())
        if ownership.unwrap():
            sides.add(side)

    # An appointed orga may also be an ordinary member of one of the teams.
    # Do not reject that member before resolving their actual override authority.
    if (
        'lan_tournament.administrate' in get_permissions_for_user(user_id)
        or tournament_orga_service.is_orga_for_tournament(user_id, tournament_id)
    ):
        return Ok(('orga', frozenset(MatchSide)))
    return Ok(('player', frozenset(sides)))


def _owns_identity(
    tournament_id: TournamentID,
    identity: ContestantIdentity,
    user_id: UserID,
) -> Result[bool, str]:
    if identity.kind == 'participant':
        participant = tournament_participant_service.find_participant(
            TournamentParticipantID(identity.id)
        )
        if (
            participant is None
            or participant.id != identity.id
            or participant.tournament_id != tournament_id
            or participant.removed_at is not None
            or participant.team_id is not None
        ):
            return Err('readiness_pairing_invalid')
        return Ok(participant.user_id == user_id)
    if identity.kind == 'team':
        team = tournament_team_service.find_team(TournamentTeamID(identity.id))
        if (
            team is None
            or team.id != identity.id
            or team.tournament_id != tournament_id
            or team.removed_at is not None
        ):
            return Err('readiness_pairing_invalid')
        members = tournament_team_service.get_team_members(team.id)
        is_current_member = any(
            member.user_id == user_id
            and member.team_id == team.id
            and member.tournament_id == tournament_id
            and member.removed_at is None
            for member in members
        )
        return Ok(team.captain_user_id == user_id and is_current_member)
    return Err('readiness_pairing_invalid')
