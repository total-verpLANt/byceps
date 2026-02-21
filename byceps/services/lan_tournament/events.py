from dataclasses import dataclass

from byceps.services.core.events import BaseEvent
from byceps.services.user.models.user import UserID

from .models.tournament import TournamentID
from .models.tournament_match import TournamentMatchID
from .models.tournament_participant import TournamentParticipantID
from .models.tournament_status import TournamentStatus
from .models.tournament_team import TournamentTeamID


# tournament


@dataclass(frozen=True, kw_only=True)
class _BaseTournamentEvent(BaseEvent):
    tournament_id: TournamentID


@dataclass(frozen=True, kw_only=True)
class TournamentCreatedEvent(_BaseTournamentEvent):
    pass


@dataclass(frozen=True, kw_only=True)
class TournamentUpdatedEvent(_BaseTournamentEvent):
    pass


@dataclass(frozen=True, kw_only=True)
class TournamentDeletedEvent(_BaseTournamentEvent):
    pass


@dataclass(frozen=True, kw_only=True)
class TournamentStatusChangedEvent(_BaseTournamentEvent):
    old_status: TournamentStatus | None
    new_status: TournamentStatus | None


# participant


@dataclass(frozen=True, kw_only=True)
class _BaseParticipantEvent(_BaseTournamentEvent):
    participant_id: TournamentParticipantID


@dataclass(frozen=True, kw_only=True)
class ParticipantJoinedEvent(_BaseParticipantEvent):
    pass


@dataclass(frozen=True, kw_only=True)
class ParticipantLeftEvent(_BaseParticipantEvent):
    pass


# team


@dataclass(frozen=True, kw_only=True)
class _BaseTeamEvent(_BaseTournamentEvent):
    team_id: TournamentTeamID


@dataclass(frozen=True, kw_only=True)
class TeamCreatedEvent(_BaseTeamEvent):
    pass


@dataclass(frozen=True, kw_only=True)
class TeamDeletedEvent(_BaseTeamEvent):
    pass


@dataclass(frozen=True, kw_only=True)
class TeamMemberJoinedEvent(_BaseTeamEvent):
    participant_id: TournamentParticipantID


@dataclass(frozen=True, kw_only=True)
class TeamMemberLeftEvent(_BaseTeamEvent):
    participant_id: TournamentParticipantID


@dataclass(frozen=True, kw_only=True)
class CaptainTransferredEvent(_BaseTeamEvent):
    old_captain_user_id: UserID
    new_captain_user_id: UserID


# match


@dataclass(frozen=True, kw_only=True)
class _BaseMatchEvent(_BaseTournamentEvent):
    match_id: TournamentMatchID


@dataclass(frozen=True, kw_only=True)
class MatchCreatedEvent(_BaseMatchEvent):
    pass


@dataclass(frozen=True, kw_only=True)
class MatchDeletedEvent(_BaseMatchEvent):
    pass


@dataclass(frozen=True, kw_only=True)
class MatchConfirmedEvent(_BaseMatchEvent):
    winner_team_id: TournamentTeamID | None
    winner_participant_id: TournamentParticipantID | None


@dataclass(frozen=True, kw_only=True)
class MatchUnconfirmedEvent(_BaseMatchEvent):
    unconfirmed_by: UserID


@dataclass(frozen=True, kw_only=True)
class ContestantAdvancedEvent(_BaseMatchEvent):
    from_match_id: TournamentMatchID
    advanced_team_id: TournamentTeamID | None
    advanced_participant_id: TournamentParticipantID | None
