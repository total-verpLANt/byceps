from dataclasses import dataclass


@dataclass(frozen=True)
class TournamentNotFoundError:
    pass


@dataclass(frozen=True)
class TournamentTeamNotFoundError:
    pass


@dataclass(frozen=True)
class TournamentParticipantNotFoundError:
    pass


@dataclass(frozen=True)
class TournamentMatchNotFoundError:
    pass


@dataclass(frozen=True)
class RegistrationNotOpenError:
    pass


@dataclass(frozen=True)
class InvalidStatusTransitionError:
    pass


@dataclass(frozen=True)
class TournamentFullError:
    pass


@dataclass(frozen=True)
class TeamFullError:
    pass


@dataclass(frozen=True)
class AlreadyParticipatingError:
    pass
