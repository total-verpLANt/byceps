from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, NewType
from uuid import UUID

from byceps.services.user.models import UserID
from .bracket import Bracket
from .tournament import TournamentID

if TYPE_CHECKING:
    from .tournament_match_to_contestant import TournamentMatchToContestant

TournamentMatchID = NewType('TournamentMatchID', UUID)


class CorrectionCase(Enum):
    """Classification of a result-correction situation.

    Named for what the correction actually faces downstream, since
    that is what decides how destructive it is.
    """

    # Nothing feeds off this match: correct it freely.
    NO_DOWNSTREAM = 'no_downstream'

    # Downstream matches exist but none is confirmed: the advanced
    # contestant is removed from them and must be re-determined.
    UNCONFIRMED_DOWNSTREAM = 'unconfirmed_downstream'

    # At least one affected downstream match is already confirmed:
    # correcting retracts it and clears its scores, so it takes an
    # explicit acknowledgement.
    CONFIRMED_DOWNSTREAM = 'confirmed_downstream'

    # The subject is the first grand final of a double-elimination
    # bracket that has been reset, and the bracket-reset match (GF M2)
    # exists. Correcting does not merely strip a contestant from it:
    # the cascade deletes GF M2 outright, with its comments and
    # contestants. That happens whether or not GF M2 is confirmed, so
    # it is reported separately from the retraction cases and takes an
    # explicit acknowledgement of its own.
    BRACKET_RESET_DELETION = 'bracket_reset_deletion'


@dataclass(frozen=True, kw_only=True)
class TournamentMatch:
    id: TournamentMatchID
    tournament_id: TournamentID
    group_order: int | None
    match_order: int | None
    round: int | None
    next_match_id: TournamentMatchID | None
    confirmed_by: UserID | None
    created_at: datetime
    bracket: Bracket | None = None
    loser_next_match_id: TournamentMatchID | None = None


@dataclass(frozen=True, kw_only=True)
class MatchUserRole:
    contestant: "TournamentMatchToContestant | None"
    is_loser: bool
    can_confirm: bool
    can_submit: bool
