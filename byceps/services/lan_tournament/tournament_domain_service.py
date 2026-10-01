from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, UTC
from math import ceil
from typing import Literal, TYPE_CHECKING
from uuid import UUID

from byceps.services.party.models import PartyID
from byceps.util.result import Err, Ok, Result
from byceps.util.uuid import generate_uuid7

from . import seed_code
from .events import (
    TournamentCreatedEvent,
    TournamentStatusChangedEvent,
)
from .models.contestant_type import ContestantType
from .models.tournament import Tournament, TournamentID
from .models.tournament_category import TournamentCategory
from .models.score_ordering import ScoreOrdering
from .models.game_format import GameFormat, is_valid_combination
from .models.elimination_mode import EliminationMode
from .models.playoff import PlayoffReleaseMode
from .models.round_robin_standing import RoundRobinStanding
from .models.tournament_match_to_contestant import (
    TournamentMatchToContestant,
)
from .models.tournament_status import TournamentStatus
from .models.validation_message import ValidationMessage

if TYPE_CHECKING:
    from .models.tournament_image import TournamentImageID
    from .models.tournament_request import TournamentRequestID


_VALID_STATUS_TRANSITIONS: dict[TournamentStatus, set[TournamentStatus]] = {
    TournamentStatus.DRAFT: {
        TournamentStatus.REGISTRATION_OPEN,
        TournamentStatus.CANCELLED,
    },
    TournamentStatus.REGISTRATION_OPEN: {
        TournamentStatus.REGISTRATION_CLOSED,
        TournamentStatus.CANCELLED,
    },
    TournamentStatus.REGISTRATION_CLOSED: {
        TournamentStatus.ONGOING,
        TournamentStatus.REGISTRATION_OPEN,
        TournamentStatus.CANCELLED,
    },
    TournamentStatus.ONGOING: {
        TournamentStatus.PAUSED,
        TournamentStatus.COMPLETED,
        TournamentStatus.CANCELLED,
    },
    TournamentStatus.PAUSED: {
        TournamentStatus.ONGOING,
        TournamentStatus.CANCELLED,
    },
    # Reopening a completed tournament. COMPLETED used to be a dead
    # end, which made a premature "Complete" unrecoverable: the only
    # code that ever left the status was the retraction cascade's
    # revert, and that fires only for a confirmed DECIDING match, so
    # a round-robin or highscore tournament -- where
    # is_deciding_match() is False by construction -- stayed frozen
    # with no route, CLI or admin action able to move it.
    #
    # Reserved for global admins: the admin blueprint's `reopen`
    # route is the only surface for it, and the site blueprint's
    # orga status actions refuse a COMPLETED tournament explicitly
    # so that its `resume` action cannot reach this edge.
    # change_status() clears the recorded winner on the way out.
    TournamentStatus.COMPLETED: {TournamentStatus.ONGOING},
    TournamentStatus.CANCELLED: set(),
}


def create_tournament(
    party_id: PartyID,
    name: str,
    *,
    game: str | None = None,
    description: str | None = None,
    image_url: str | None = None,
    ruleset: str | None = None,
    start_time: datetime | None = None,
    min_players: int | None = None,
    max_players: int | None = None,
    min_teams: int | None = None,
    max_teams: int | None = None,
    min_players_in_team: int | None = None,
    max_players_in_team: int | None = None,
    contestant_type: ContestantType | None = None,
    tournament_status: TournamentStatus | None = None,
    game_format: GameFormat | None = None,
    elimination_mode: EliminationMode | None = None,
    score_ordering: ScoreOrdering | None = None,
    point_table: list[int] | None = None,
    advancement_count: int | None = None,
    group_size_min: int | None = None,
    group_size_max: int | None = None,
    points_carry_to_losers: bool | None = None,
    playoff_game_format: GameFormat | None = None,
    playoff_elimination_mode: EliminationMode | None = None,
    playoff_group_count: int | None = None,
    playoff_qualifiers_per_group: int | None = None,
    playoff_qualifier_count: int | None = None,
    playoff_release_mode: PlayoffReleaseMode | None = None,
    position: int = 0,
    category: TournamentCategory = TournamentCategory.MAIN,
    created_from_request_id: 'TournamentRequestID | None' = None,
    image_id: 'TournamentImageID | None' = None,
    image_alt_text: str | None = None,
    creation_token: UUID | None = None,
) -> tuple[Tournament, TournamentCreatedEvent]:
    """Create a new tournament."""
    tournament_id = TournamentID(generate_uuid7())
    now = datetime.now(UTC)

    tournament = Tournament(
        id=tournament_id,
        party_id=party_id,
        name=name,
        game=game,
        description=description,
        image_url=image_url,
        ruleset=ruleset,
        start_time=start_time,
        created_at=now,
        min_players=min_players,
        max_players=max_players,
        min_teams=min_teams,
        max_teams=max_teams,
        min_players_in_team=min_players_in_team,
        max_players_in_team=max_players_in_team,
        contestant_type=contestant_type,
        tournament_status=tournament_status,
        game_format=game_format,
        elimination_mode=elimination_mode,
        score_ordering=score_ordering,
        point_table=point_table,
        advancement_count=advancement_count,
        group_size_min=group_size_min,
        group_size_max=group_size_max,
        points_carry_to_losers=points_carry_to_losers,
        playoff_game_format=playoff_game_format,
        playoff_elimination_mode=playoff_elimination_mode,
        playoff_group_count=playoff_group_count,
        playoff_qualifiers_per_group=playoff_qualifiers_per_group,
        playoff_qualifier_count=playoff_qualifier_count,
        playoff_release_mode=playoff_release_mode,
        position=position,
        category=category,
        created_from_request_id=created_from_request_id,
        image_id=image_id,
        image_alt_text=image_alt_text,
        creation_token=creation_token,
    )

    event = TournamentCreatedEvent(
        occurred_at=now,
        initiator=None,
        tournament_id=tournament_id,
    )

    return tournament, event


def derive_contestant_type(
    contestant_type: ContestantType | None,
    max_players_in_team: int | None,
    min_players_in_team: int | None,
) -> ContestantType:
    """Derive the contestant type from team size when not set explicitly.

    An explicit `contestant_type` is returned unchanged. Otherwise, the
    team size (`max_players_in_team`, falling back to
    `min_players_in_team`) decides: bigger than 1 means TEAM; 1, or no
    team size at all, means SOLO.
    """
    if contestant_type is not None:
        return contestant_type

    team_size = (
        max_players_in_team
        if max_players_in_team is not None
        else min_players_in_team
    )
    if team_size is not None and team_size > 1:
        return ContestantType.TEAM

    return ContestantType.SOLO


@dataclass(frozen=True, kw_only=True)
class TournamentSettings:
    contestant_type: ContestantType | None
    game_format: GameFormat | None
    elimination_mode: EliminationMode | None
    score_ordering: ScoreOrdering | None
    min_players: int | None
    max_players: int | None
    min_teams: int | None
    max_teams: int | None
    min_players_in_team: int | None
    max_players_in_team: int | None
    point_table: list[int] | None
    group_size_min: int | None
    group_size_max: int | None
    advancement_count: int | None
    playoff_game_format: GameFormat | None = None
    playoff_elimination_mode: EliminationMode | None = None
    playoff_group_count: int | None = None
    playoff_qualifiers_per_group: int | None = None
    playoff_qualifier_count: int | None = None
    playoff_release_mode: PlayoffReleaseMode | None = None


MAX_POINT_TABLE_PLACES = 64

# Equals `tournament_match_service.MAX_MATCH_SCORE`. A placement's
# points are stored in a 32-bit column when a group is confirmed.
MAX_POINTS_PER_PLACE = 999_999_999

# The seed code stores the parameter in 8 bits (`seed_code.MAX_PARAM`).
MAX_PLAYOFF_GROUP_COUNT = seed_code.MAX_PARAM
MAX_LOBBY_SIZE = seed_code.MAX_PARAM


POINTS_TOO_HIGH_MSGID = 'Points may be at most %(max)s.'

POINTS_TOO_LOW_MSGID = 'Points may be at least %(min)s.'

TOO_MANY_PLACES_MSGID = 'At most %(max)s places.'


def game_format_for_phase(
    tournament: Tournament, phase: int
) -> GameFormat | None:
    """Return the game format that `phase` of the tournament runs."""
    if phase == 1:
        return tournament.game_format
    if phase == 2:
        return tournament.playoff_game_format
    return None


def elimination_mode_for_phase(
    tournament: Tournament, phase: int
) -> EliminationMode | None:
    """Return the elimination mode that `phase` of the tournament runs."""
    if phase == 1:
        return tournament.elimination_mode
    if phase == 2:
        return tournament.playoff_elimination_mode
    return None


def check_point_count(point_table: list[int]) -> ValidationMessage | None:
    """Return a message if the table has more places than allowed."""
    if len(point_table) > MAX_POINT_TABLE_PLACES:
        return ValidationMessage(
            TOO_MANY_PLACES_MSGID, (('max', MAX_POINT_TABLE_PLACES),)
        )
    return None


def check_point_values(point_table: list[int]) -> ValidationMessage | None:
    """Return a message if a place is worth more or less than allowed."""
    if any(points > MAX_POINTS_PER_PLACE for points in point_table):
        return ValidationMessage(
            POINTS_TOO_HIGH_MSGID, (('max', MAX_POINTS_PER_PLACE),)
        )
    if any(points < -MAX_POINTS_PER_PLACE for points in point_table):
        return ValidationMessage(
            POINTS_TOO_LOW_MSGID, (('min', -MAX_POINTS_PER_PLACE),)
        )
    return None


def validate_tournament_settings(
    settings: TournamentSettings, *, require_structure: bool
) -> Result[None, dict[str, ValidationMessage]]:
    """Check structural and cross-field tournament rules."""
    errors: dict[str, ValidationMessage] = {}

    def add(field: str, msgid: str, **params: str | int) -> None:
        errors.setdefault(
            field, ValidationMessage(msgid, tuple(params.items()))
        )

    def check_pair(min_field: str, max_field: str, min_label: str) -> None:
        min_value = getattr(settings, min_field)
        max_value = getattr(settings, max_field)
        if (
            min_value is not None
            and max_value is not None
            and min_value > max_value
        ):
            add(
                max_field,
                'Must be at least "%(other)s" (%(n)s).',
                other=min_label,
                n=min_value,
            )

    contestant_type = settings.contestant_type
    game_format = settings.game_format
    elimination_mode = settings.elimination_mode

    if require_structure:
        if contestant_type is None:
            add(
                'contestant_type',
                'Please choose whether individuals or teams compete.',
            )
        if game_format is None:
            add('game_format', 'Please choose a game format.')
        if elimination_mode is None and game_format != GameFormat.HIGHSCORE:
            add('elimination_mode', 'Please choose an elimination mode.')

    if (
        game_format is not None
        and elimination_mode is not None
        and not is_valid_combination(game_format, elimination_mode)
    ):
        add(
            'elimination_mode',
            'This combination of game format and elimination mode is not '
            'supported.',
        )

    if contestant_type == ContestantType.SOLO:
        check_pair('min_players', 'max_players', 'Min. players')
    elif contestant_type == ContestantType.TEAM:
        check_pair('min_teams', 'max_teams', 'Min. teams')
        check_pair(
            'min_players_in_team',
            'max_players_in_team',
            'Min. players per team',
        )

    if game_format == GameFormat.HIGHSCORE and settings.score_ordering is None:
        add('score_ordering', 'Please choose which results are better.')

    if game_format == GameFormat.FREE_FOR_ALL:
        _check_free_for_all(settings, add)

    _check_playoffs(settings, add)

    if ffa_cut_missing(settings):
        add('advancement_count', FFA_CUT_REQUIRED_MSGID)

    if errors:
        return Err(errors)
    return Ok(None)


def validate_playoff_settings(
    settings: TournamentSettings,
) -> Result[None, dict[str, ValidationMessage]]:
    """Check the playoff rules alone, for callers without a form."""
    errors: dict[str, ValidationMessage] = {}

    def add(field: str, msgid: str, **params: str | int) -> None:
        errors.setdefault(
            field, ValidationMessage(msgid, tuple(params.items()))
        )

    _check_playoffs(settings, add)

    if errors:
        return Err(errors)
    return Ok(None)


NO_PLAYOFF_PHASE_MSGID = 'This format has no playoff phase.'

_PLAYOFF_MODES = frozenset(
    {EliminationMode.SINGLE_ELIMINATION, EliminationMode.DOUBLE_ELIMINATION}
)


def _check_playoffs(
    settings: TournamentSettings, add: Callable[..., None]
) -> None:
    """Add the playoff rules of `validate_tournament_settings`."""
    fields = (
        settings.playoff_game_format,
        settings.playoff_elimination_mode,
        settings.playoff_group_count,
        settings.playoff_qualifiers_per_group,
        settings.playoff_qualifier_count,
        settings.playoff_release_mode,
    )
    if all(value is None for value in fields):
        return

    is_round_robin = (
        settings.game_format == GameFormat.ONE_V_ONE
        and settings.elimination_mode == EliminationMode.ROUND_ROBIN
    )
    is_highscore = settings.game_format == GameFormat.HIGHSCORE
    if not is_round_robin and not is_highscore:
        add('playoff_game_format', NO_PLAYOFF_PHASE_MSGID)
        return

    if is_round_robin:
        required_format = GameFormat.ONE_V_ONE
        wrong_format = 'Round robin playoffs must be 1v1.'
    else:
        required_format = GameFormat.FREE_FOR_ALL
        wrong_format = 'Highscore playoffs must be Free-for-All.'

    playoff_format = settings.playoff_game_format
    if playoff_format is None:
        add('playoff_game_format', 'Please choose the playoff format.')
    elif playoff_format != required_format:
        add('playoff_game_format', wrong_format)

    playoff_mode = settings.playoff_elimination_mode
    if playoff_mode is None:
        add(
            'playoff_elimination_mode',
            'Please choose a playoff elimination mode.',
        )
    elif playoff_mode not in _PLAYOFF_MODES:
        add(
            'playoff_elimination_mode',
            'Playoffs must use single or double elimination.',
        )

    if settings.playoff_release_mode is None:
        add(
            'playoff_release_mode',
            'Please choose how the playoffs are released.',
        )

    if is_round_robin:
        _check_round_robin_playoffs(settings, add)
    else:
        _check_highscore_playoffs(settings, add)


def _check_round_robin_playoffs(
    settings: TournamentSettings, add: Callable[..., None]
) -> None:
    """Add the group and qualifier rules for round robin playoffs."""
    groups = settings.playoff_group_count
    per_group = settings.playoff_qualifiers_per_group

    if settings.playoff_qualifier_count is not None:
        add(
            'playoff_qualifier_count',
            'Only used for highscore playoffs.',
        )

    if groups is None:
        add('playoff_group_count', 'Please enter the number of groups.')
    elif groups < 2:
        add('playoff_group_count', 'At least two groups are needed.')
    elif groups > MAX_PLAYOFF_GROUP_COUNT:
        add(
            'playoff_group_count',
            'At most %(max)s.',
            max=MAX_PLAYOFF_GROUP_COUNT,
        )

    if per_group is None:
        add(
            'playoff_qualifiers_per_group',
            'Please enter how many advance from each group.',
        )
    elif per_group < 1:
        add(
            'playoff_qualifiers_per_group',
            'At least one contestant must advance from each group.',
        )

    if (
        groups is None
        or per_group is None
        or groups < 2
        or groups > MAX_PLAYOFF_GROUP_COUNT
        or per_group < 1
    ):
        return

    if settings.contestant_type == ContestantType.TEAM:
        minimum = settings.min_teams
    else:
        minimum = settings.min_players
    if minimum is not None:
        smallest = minimum // groups
        if smallest < 2:
            add(
                'playoff_group_count',
                'The minimum number of contestants is too small for this '
                'many groups.',
            )
            return
        if per_group >= smallest:
            add(
                'playoff_qualifiers_per_group',
                'Fewer must advance from each group than the smallest '
                'group holds.',
            )
            return

    if settings.contestant_type == ContestantType.TEAM:
        maximum = settings.max_teams
    else:
        maximum = settings.max_players
    if maximum is not None and groups > maximum // 2:
        add(
            'playoff_group_count',
            'The maximum number of contestants is too small for this many '
            'groups.',
        )
        return

    if settings.playoff_elimination_mode == EliminationMode.DOUBLE_ELIMINATION:
        if groups * per_group < 4:
            add(
                'playoff_qualifiers_per_group',
                'Double elimination playoffs need at least 4 qualifiers '
                'in total.',
            )
    elif groups * per_group < 2:
        add(
            'playoff_qualifiers_per_group',
            'Playoffs need at least 2 qualifiers in total.',
        )


QUALIFIERS_SPLIT_MSGID = (
    'The qualifiers cannot be split into lobbies between the minimum '
    'and maximum group size.'
)


FFA_CUT_REQUIRED_MSGID = 'Please enter how many advance per lobby.'


def ffa_cut_missing(settings: TournamentSettings) -> bool:
    """Tell whether an FFA phase needs a cut it does not have.

    Double elimination always needs one. Single elimination needs one
    when more than one lobby can form: no bound on the contestants, or
    a bound above the largest lobby. The bound of highscore playoffs is
    the qualifier count.
    """
    if (
        settings.advancement_count is not None
        or settings.group_size_max is None
    ):
        return False
    if settings.game_format == GameFormat.FREE_FOR_ALL:
        mode = settings.elimination_mode
        bound = (
            settings.max_teams
            if settings.contestant_type == ContestantType.TEAM
            else settings.max_players
        )
    elif (
        settings.game_format == GameFormat.HIGHSCORE
        and settings.playoff_game_format == GameFormat.FREE_FOR_ALL
    ):
        mode = settings.playoff_elimination_mode
        bound = settings.playoff_qualifier_count
    else:
        return False
    if mode == EliminationMode.DOUBLE_ELIMINATION:
        return True
    return bound is None or bound > settings.group_size_max


def ffa_lobbies_fit(count: int, group_min: int, group_max: int) -> bool:
    """Tell whether `count` contestants make lobbies of `group_min` to `group_max`.

    As few lobbies as the maximum allows, sizes differing by at most one,
    like `snake_seed_groups` and the seeding layout.
    """
    lobbies = ceil(count / group_max)
    return count // lobbies >= group_min


FFA_NO_PROGRESS_MSGID = (
    'With %(count)s contestants, round %(round)s would send everyone on: '
    'lobbies of %(sizes)s with %(cut)s advancing per lobby never shrink. '
    'Lower the number advancing per lobby or raise the minimum lobby size.'
)
FFA_STALLS_MSGID = (
    'With %(count)s contestants, round %(round)s would need lobbies of '
    '%(sizes)s, below the minimum of %(minimum)s. Change the number '
    'advancing per lobby or the lobby sizes.'
)
HIGHSCORE_FFA_NO_PROGRESS_MSGID = (
    'With %(count)s qualifiers, round %(round)s would send everyone on: '
    'lobbies of %(sizes)s with %(cut)s advancing per lobby never shrink. '
    'Lower the number advancing per lobby, raise the minimum lobby size '
    'or change the qualifiers.'
)
HIGHSCORE_FFA_STALLS_MSGID = (
    'With %(count)s qualifiers, round %(round)s would need lobbies of '
    '%(sizes)s, below the minimum of %(minimum)s. Change the number '
    'advancing per lobby, the lobby sizes or the qualifiers.'
)


@dataclass(frozen=True, kw_only=True)
class FfaDeadEnd:
    reason: Literal['below_minimum', 'no_progress']
    round_number: int  # 0-based round that cannot be formed or repeats
    count: int
    lobby_sizes: tuple[int, ...]  # descending
    minimum: int


def _ffa_lobby_split(count: int, group_max: int) -> tuple[int, ...]:
    """Return lobby sizes as `snake_seed_groups` and the seeding layout cut them."""
    lobbies = max(1, ceil(count / max(2, group_max)))
    base, extra = divmod(count, lobbies)
    return (base + 1,) * extra + (base,) * (lobbies - extra)


def ffa_single_track_dead_end(
    count: int, group_min: int | None, group_max: int, cut: int
) -> FfaDeadEnd | None:
    """Return the first later single-track round whose lobbies fall below the minimum."""
    minimum = max(2, group_min or 2)
    round_number = 0
    while True:
        sizes = _ffa_lobby_split(count, group_max)
        if len(sizes) == 1:
            return None
        if round_number > 0 and sizes[-1] < minimum:
            return FfaDeadEnd(
                reason='below_minimum',
                round_number=round_number,
                count=count,
                lobby_sizes=sizes,
                minimum=minimum,
            )
        next_count = sum(min(cut, size) for size in sizes)
        if next_count >= count:
            return FfaDeadEnd(
                reason='no_progress',
                round_number=round_number + 1,
                count=next_count,
                lobby_sizes=_ffa_lobby_split(next_count, group_max),
                minimum=minimum,
            )
        count, round_number = next_count, round_number + 1


def _check_highscore_playoffs(
    settings: TournamentSettings, add: Callable[..., None]
) -> None:
    """Add the qualifier rules for highscore playoffs."""
    qualifiers = settings.playoff_qualifier_count

    if (
        settings.playoff_group_count is not None
        or settings.playoff_qualifiers_per_group is not None
    ):
        add(
            'playoff_group_count',
            'Only used for round robin playoffs.',
        )

    if qualifiers is None:
        add('playoff_qualifier_count', 'Please enter the number of qualifiers.')
    elif qualifiers < 2:
        add('playoff_qualifier_count', 'At least two qualifiers are needed.')
    elif (
        settings.group_size_min is not None
        and qualifiers < settings.group_size_min
    ):
        add(
            'playoff_qualifier_count',
            'Qualifiers must be at least the minimum group size.',
        )
    elif (
        settings.group_size_max is not None
        and (settings.group_size_min or 2) <= settings.group_size_max
        and not ffa_lobbies_fit(
            qualifiers, settings.group_size_min or 2, settings.group_size_max
        )
    ):
        add('playoff_qualifier_count', QUALIFIERS_SPLIT_MSGID)

    if (
        settings.playoff_elimination_mode is EliminationMode.SINGLE_ELIMINATION
        and settings.advancement_count is not None
        and settings.group_size_max is not None
        and qualifiers is not None
        and not (
            qualifiers < 2
            or (
                settings.group_size_min is not None
                and qualifiers < settings.group_size_min
            )
        )
        and (
            dead_end := ffa_single_track_dead_end(
                qualifiers,
                settings.group_size_min,
                settings.group_size_max,
                settings.advancement_count,
            )
        )
    ):
        add(
            'playoff_qualifier_count',
            HIGHSCORE_FFA_STALLS_MSGID
            if dead_end.reason == 'below_minimum'
            else HIGHSCORE_FFA_NO_PROGRESS_MSGID,
            cut=settings.advancement_count,
            count=dead_end.count,
            round=dead_end.round_number + 1,
            sizes=', '.join(map(str, dead_end.lobby_sizes)),
            minimum=dead_end.minimum,
        )

    _check_free_for_all(settings, add)


def _check_free_for_all(
    settings: TournamentSettings, add: Callable[..., None]
) -> None:
    """Add the Free-for-All rules of `validate_tournament_settings`."""
    is_team = settings.contestant_type == ContestantType.TEAM
    point_table = settings.point_table
    group_min = settings.group_size_min
    group_max = settings.group_size_max
    advancement = settings.advancement_count

    if not point_table:
        add('point_table', 'Add points for at least place 1.')
    elif (too_many := check_point_count(point_table)) is not None:
        add('point_table', too_many.msgid, **dict(too_many.params))
    elif (out_of_range := check_point_values(point_table)) is not None:
        add('point_table', out_of_range.msgid, **dict(out_of_range.params))

    group_min_too_large = False
    if group_max is None:
        add('group_size_max', 'Required for Free-for-All.')
    elif group_max > MAX_LOBBY_SIZE:
        add('group_size_max', 'At most %(max)s.', max=MAX_LOBBY_SIZE)
    elif group_min is not None and group_min > group_max:
        group_min_too_large = True
        add(
            'group_size_min',
            'Must not be larger than the max. group size (%(n)s).',
            n=group_max,
        )

    max_contestants = settings.max_teams if is_team else settings.max_players
    need = group_min if group_min is not None else 2
    if (
        not group_min_too_large
        and max_contestants is not None
        and max_contestants < need
    ):
        if is_team:
            add(
                'group_size_min',
                'With at most %(n)s teams no group of at least %(min)s teams '
                'can form. Lower the minimum to %(n)s or raise '
                '"Max. teams" in step 3.',
                n=max_contestants,
                min=need,
            )
        else:
            add(
                'group_size_min',
                'With at most %(n)s players no group of at least %(min)s '
                'can form. Lower the minimum or raise "Max. players" in '
                'step 3.',
                n=max_contestants,
                min=need,
            )

    if advancement is not None:
        smallest = (
            group_min
            if group_min is not None and not group_min_too_large
            else group_max
        )
        if smallest is not None and advancement >= smallest:
            if is_team:
                add(
                    'advancement_count',
                    'Fewer than %(n)s must advance from the smallest group '
                    '(%(n)s teams).',
                    n=smallest,
                )
            else:
                add(
                    'advancement_count',
                    'Fewer than %(n)s must advance from the smallest group '
                    '(%(n)s players).',
                    n=smallest,
                )


def validate_status_transition(
    current_status: TournamentStatus | None,
    new_status: TournamentStatus,
) -> Result[TournamentStatus, str]:
    """Validate that a status transition is allowed."""
    if current_status is None:
        return Ok(new_status)

    allowed = _VALID_STATUS_TRANSITIONS.get(current_status, set())
    if new_status not in allowed:
        # Keep this a static msgid; the view translates it.
        return Err(
            'Cannot transition the tournament to the requested '
            'status from its current status.'
        )

    return Ok(new_status)


def change_tournament_status(
    tournament: Tournament,
    new_status: TournamentStatus,
) -> Result[tuple[TournamentStatusChangedEvent], str]:
    """Validate and produce a status change event."""
    result = validate_status_transition(
        tournament.tournament_status, new_status
    )
    if result.is_err():
        return Err(result.unwrap_err())

    now = datetime.now(UTC)

    event = TournamentStatusChangedEvent(
        occurred_at=now,
        initiator=None,
        tournament_id=tournament.id,
        old_status=tournament.tournament_status,
        new_status=new_status,
    )

    return Ok((event,))


def validate_participant_count(
    tournament: Tournament,
    current_count: int,
) -> Result[None, str]:
    """Check if the tournament can accept more participants."""
    if (
        tournament.max_players is not None
        and current_count >= tournament.max_players
    ):
        return Err('Tournament is full.')

    return Ok(None)


def validate_team_count(
    tournament: Tournament,
    current_count: int,
) -> Result[None, str]:
    """Check if the tournament can accept more teams."""
    if (
        tournament.max_teams is not None
        and current_count >= tournament.max_teams
    ):
        return Err('Maximum number of teams reached.')

    return Ok(None)


def determine_match_winner(
    contestants: list[TournamentMatchToContestant],
) -> Result[TournamentMatchToContestant | None, str]:
    """Determine the winner of a match by highest score.

    Returns ``Ok(winner)`` when there is a clear winner,
    ``Ok(None)`` when the match is a draw (equal scores),
    or ``Err(reason)`` on validation failure.
    """
    if len(contestants) < 2:
        return Err('Need at least 2 contestants to determine winner.')

    for contestant in contestants:
        if contestant.score is None:
            return Err('All contestants must have scores.')

    sorted_contestants = sorted(
        contestants, key=lambda c: c.score, reverse=True
    )

    if sorted_contestants[0].score == sorted_contestants[1].score:
        return Ok(None)

    return Ok(sorted_contestants[0])


def generate_round_robin_schedule(
    contestant_ids: list[str],
) -> list[list[tuple[str | None, str | None]]]:
    """Generate round-robin schedule using circle method."""
    players: list[str | None] = list(contestant_ids)
    if len(players) % 2 == 1:
        players.append(None)  # defwin

    n = len(players)
    num_rounds = n - 1
    schedule: list[list[tuple[str | None, str | None]]] = []

    for _round_num in range(num_rounds):
        round_matches: list[tuple[str | None, str | None]] = []
        top = players[: n // 2]
        bottom = players[n // 2 :][::-1]
        for p1, p2 in zip(top, bottom, strict=True):
            if p1 is not None and p2 is not None:
                round_matches.append((p1, p2))
        schedule.append(round_matches)
        players = [players[0]] + [players[-1]] + players[1:-1]

    return schedule


def contestant_id(
    c: TournamentMatchToContestant,
) -> str:
    """Return the effective contestant ID as a string."""
    if c.participant_id is None and c.team_id is None:
        raise ValueError(
            'Contestant has neither participant_id'
            ' nor team_id.'
        )
    return str(
        c.participant_id
        if c.participant_id is not None
        else c.team_id
    )


def compute_round_robin_standings(
    matches: list[list[TournamentMatchToContestant]],
) -> list[RoundRobinStanding]:
    """Compute round-robin standings from confirmed matches.

    Each element in *matches* is a list of one or two
    ``TournamentMatchToContestant`` entries representing one
    completed match. A one-entry walkover is a win without scores.
    The caller filters out unconfirmed matches.
    Points are awarded as: Win = 3, Draw = 1,
    Loss = 0.  The returned list is sorted by points DESC,
    score_diff DESC, score_for DESC.
    """
    stats: dict[str, dict[str, int]] = {}

    def _ensure(cid: str) -> dict[str, int]:
        if cid not in stats:
            stats[cid] = {
                'points': 0,
                'wins': 0,
                'draws': 0,
                'losses': 0,
                'score_for': 0,
                'score_against': 0,
            }
        return stats[cid]

    for contestants in matches:
        if len(contestants) == 1:
            stats_row = _ensure(contestant_id(contestants[0]))
            stats_row['wins'] += 1
            stats_row['points'] += 3
            continue
        if len(contestants) != 2:
            continue

        c1, c2 = contestants
        cid1 = contestant_id(c1)
        cid2 = contestant_id(c2)

        s1 = _ensure(cid1)
        s2 = _ensure(cid2)

        score1 = c1.score if c1.score is not None else 0
        score2 = c2.score if c2.score is not None else 0

        s1['score_for'] += score1
        s1['score_against'] += score2
        s2['score_for'] += score2
        s2['score_against'] += score1

        winner_result = determine_match_winner(contestants)
        if winner_result.is_err():
            # Validation failure — skip this match.
            continue

        winner = winner_result.unwrap()
        if winner is None:
            # Draw (equal scores).
            s1['draws'] += 1
            s1['points'] += 1
            s2['draws'] += 1
            s2['points'] += 1
        else:
            winner_id = contestant_id(winner)
            if winner_id == cid1:
                s1['wins'] += 1
                s1['points'] += 3
                s2['losses'] += 1
            else:
                s2['wins'] += 1
                s2['points'] += 3
                s1['losses'] += 1

    standings = [
        RoundRobinStanding(
            contestant_id=cid,
            points=s['points'],
            wins=s['wins'],
            draws=s['draws'],
            losses=s['losses'],
            score_for=s['score_for'],
            score_against=s['score_against'],
            score_diff=s['score_for'] - s['score_against'],
        )
        for cid, s in stats.items()
    ]

    standings.sort(
        key=lambda s: (s.points, s.score_diff, s.score_for),
        reverse=True,
    )

    return standings


def _standard_seed_order(bracket_size: int) -> list[int]:
    """Return standard seeding order for single elimination.

    For bracket_size=8: [0, 7, 3, 4, 1, 6, 2, 5]
    This produces matchups: 1v8, 4v5, 2v7, 3v6 (1-indexed).
    """
    if bracket_size == 1:
        return [0]

    if bracket_size == 2:
        return [0, 1]

    half = bracket_size // 2
    prev = _standard_seed_order(half)
    result = []
    for seed in prev:
        result.append(seed)
        result.append(bracket_size - 1 - seed)
    return result


# -------------------------------------------------------------------- #
# FFA domain logic
# -------------------------------------------------------------------- #

# Keep this a static msgid; the views translate it without parameters.
GROUP_BELOW_MINIMUM_ERROR = (
    'A group has fewer contestants than the minimum group size.'
)


def snake_seed_groups(
    contestant_ids: list[str],
    group_size_min: int,
    group_size_max: int,
) -> Result[list[list[str]], str]:
    """Distribute contestants into groups using serpentine/snake ordering.

    Groups are as equal as possible (sizes differ by at most 1).
    Returns ``Err`` when the input is empty or any resulting group
    would be smaller than *group_size_min*.
    """
    if not contestant_ids:
        return Err('No contestants to distribute.')

    num_groups = ceil(len(contestant_ids) / group_size_max)
    groups: list[list[str]] = [[] for _ in range(num_groups)]

    for row_idx, cid in enumerate(contestant_ids):
        group_in_row = row_idx % num_groups
        row_number = row_idx // num_groups
        # Even rows go L->R, odd rows go R->L (serpentine).
        if row_number % 2 == 0:
            target = group_in_row
        else:
            target = num_groups - 1 - group_in_row
        groups[target].append(cid)

    # Validate minimum group size.
    for group in groups:
        if len(group) < group_size_min:
            return Err(GROUP_BELOW_MINIMUM_ERROR)

    return Ok(groups)


def map_placement_to_points(placement: int, point_table: list[int]) -> int:
    """Map a 1-indexed placement to points from the given table.

    Returns ``0`` for placements beyond the table length.
    """
    idx = placement - 1
    if 0 <= idx < len(point_table):
        return point_table[idx]
    return 0


def compute_ffa_round_standings(
    matches: list[list[TournamentMatchToContestant]],
) -> list[tuple[str, int]]:
    """Compute per-player standings from a single FFA round.

    *matches* is a list of groups, where each group is a list of
    ``TournamentMatchToContestant`` entries with ``placement`` and
    ``points`` already set.

    Returns a list of ``(contestant_id, points)`` tuples sorted by
    points descending.
    """
    standings: dict[str, int] = {}

    for group in matches:
        for contestant in group:
            cid = contestant_id(contestant)
            pts = contestant.points if contestant.points is not None else 0
            standings[cid] = standings.get(cid, 0) + pts

    result = sorted(standings.items(), key=lambda item: item[1], reverse=True)
    return result


def compute_ffa_cumulative_standings(
    all_round_matches: list[list[list[TournamentMatchToContestant]]],
) -> list[tuple[str, int]]:
    """Aggregate FFA standings across all rounds.

    *all_round_matches* is a list of rounds, where each round is
    structured as in :func:`compute_ffa_round_standings`.

    Returns a list of ``(contestant_id, total_points)`` tuples sorted
    by total points descending.
    """
    cumulative: dict[str, int] = {}

    for round_matches in all_round_matches:
        round_standings = compute_ffa_round_standings(round_matches)
        for cid, pts in round_standings:
            cumulative[cid] = cumulative.get(cid, 0) + pts

    result = sorted(
        cumulative.items(), key=lambda item: item[1], reverse=True
    )
    return result
