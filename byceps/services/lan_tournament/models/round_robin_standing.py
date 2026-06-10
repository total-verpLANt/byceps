from dataclasses import dataclass


@dataclass(frozen=True, kw_only=True)
class RoundRobinStanding:
    """Standings entry for a round-robin tournament contestant.

    `contestant_id` is `str` to abstract over both participant and team
    IDs; callers resolve it to `TournamentParticipantID` or
    `TournamentTeamID` as needed. Points: Win = 3, Draw = 1, Loss = 0.
    """

    contestant_id: str
    points: int
    wins: int
    draws: int
    losses: int
    score_for: int
    score_against: int
    score_diff: int
