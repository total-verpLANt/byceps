"""
byceps.services.lan_tournament.tournament_config_domain_service
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from dataclasses import dataclass
from datetime import datetime, UTC
from enum import Enum
from typing import Any

from byceps.services.lan_tournament import (
    tournament_domain_service,
    tournament_request_domain_service,
)
from byceps.services.lan_tournament.models.contestant_type import (
    ContestantType,
)
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.playoff import PlayoffReleaseMode
from byceps.services.lan_tournament.models.score_ordering import ScoreOrdering
from byceps.services.lan_tournament.models.tournament import Tournament
from byceps.services.lan_tournament.models.tournament_category import (
    TournamentCategory,
)
from byceps.services.lan_tournament.models.validation_message import (
    ValidationMessage,
)
from byceps.util.result import Err, Ok, Result


MAX_NAME_LENGTH = 80
MAX_GAME_LENGTH = 80
MAX_TEXT_LENGTH = 10_000
MAX_COUNT = tournament_request_domain_service.MAX_PARTICIPANT_LIMIT

NAME_REQUIRED_ERROR = 'Please enter a name.'
LENGTH_ERROR = 'At most %(max)d characters – currently %(length)d.'
CATEGORY_INVALID_ERROR = 'Please choose a valid tournament category.'
WHOLE_NUMBER_ERROR = 'Whole numbers from 1 only.'
NUMBER_AT_LEAST_ERROR = 'Number must be at least %(min)s.'
AT_MOST_ERROR = 'At most %(max)s.'
AT_LEAST_TWO_ERROR = 'At least 2.'
MIN_ABOVE_MAX_ERROR = 'Must be at least "%(other)s" (%(n)s).'
POINT_TABLE_TEXT_ERROR = 'Point table must be comma-separated integers.'
CONTESTANT_TYPE_INVALID_ERROR = 'Invalid contestant type selected.'
GAME_FORMAT_INVALID_ERROR = 'Invalid game format selected.'
ELIMINATION_MODE_INVALID_ERROR = 'Invalid elimination mode selected.'
SCORE_ORDERING_INVALID_ERROR = 'Invalid score ordering selected.'
RELEASE_MODE_INVALID_ERROR = 'Invalid release mode selected.'
UNSTORABLE_CHARACTER_ERROR = (
    'The text contains a character that cannot be stored'
    ' (NUL or an unpaired surrogate).'
)

PLAYOFF_KWARGS = (
    'playoff_game_format',
    'playoff_elimination_mode',
    'playoff_group_count',
    'playoff_qualifiers_per_group',
    'playoff_qualifier_count',
    'playoff_release_mode',
)

_WHOLE_NUMBER = ValidationMessage(WHOLE_NUMBER_ERROR)
_AT_LEAST_ONE = ValidationMessage(NUMBER_AT_LEAST_ERROR, (('min', 1),))
_AT_LEAST_TWO = ValidationMessage(AT_LEAST_TWO_ERROR)

# Field, lowest value, message below it, highest value.
_COUNT_RULES = (
    ('min_players', 1, _WHOLE_NUMBER, MAX_COUNT),
    ('max_players', 1, _WHOLE_NUMBER, MAX_COUNT),
    ('min_teams', 1, _WHOLE_NUMBER, MAX_COUNT),
    ('max_teams', 1, _WHOLE_NUMBER, MAX_COUNT),
    ('min_players_in_team', 1, _WHOLE_NUMBER, MAX_COUNT),
    ('max_players_in_team', 1, _WHOLE_NUMBER, MAX_COUNT),
    ('group_size_min', 2, _AT_LEAST_TWO, MAX_COUNT),
    (
        'group_size_max',
        2,
        _AT_LEAST_TWO,
        tournament_domain_service.MAX_LOBBY_SIZE,
    ),
    ('advancement_count', 1, _AT_LEAST_ONE, MAX_COUNT),
    (
        'playoff_group_count',
        1,
        _WHOLE_NUMBER,
        tournament_domain_service.MAX_PLAYOFF_GROUP_COUNT,
    ),
    ('playoff_qualifiers_per_group', 1, _WHOLE_NUMBER, MAX_COUNT),
    ('playoff_qualifier_count', 1, _WHOLE_NUMBER, MAX_COUNT),
)

# Min field, max field and the msgid of the min field's label.
_MIN_MAX_PAIRS = (
    ('min_players', 'max_players', 'Min. players'),
    ('min_teams', 'max_teams', 'Min. teams'),
    ('min_players_in_team', 'max_players_in_team', 'Min. players per team'),
)


@dataclass(frozen=True, kw_only=True)
class TournamentConfigInput:
    """Create settings as submitted, before normalisation."""

    name: str
    category: str | None
    game: str | None = None
    description: str | None = None
    ruleset: str | None = None
    start_time: datetime | None = None  # naive UTC
    contestant_type: str | None = None
    game_format: str | None = None
    elimination_mode: str | None = None
    score_ordering: str | None = None
    min_players: int | None = None
    max_players: int | None = None
    min_teams: int | None = None
    max_teams: int | None = None
    min_players_in_team: int | None = None
    max_players_in_team: int | None = None
    point_table: list[int] | str | None = None
    advancement_count: int | None = None
    group_size_min: int | None = None
    group_size_max: int | None = None
    points_carry_to_losers: bool = False
    playoff_enabled: bool = False
    playoff_group_count: int | None = None
    playoff_qualifiers_per_group: int | None = None
    playoff_qualifier_count: int | None = None
    playoff_elimination_mode: str | None = None
    playoff_release_mode: str | None = None


@dataclass(frozen=True, kw_only=True)
class TournamentConfig:
    """Normalised create settings."""

    name: str
    category: TournamentCategory
    game: str | None
    description: str | None
    ruleset: str | None
    start_time: datetime | None
    settings: tournament_domain_service.TournamentSettings
    points_carry_to_losers: bool | None


def normalize_config(
    config_input: TournamentConfigInput,
) -> Result[TournamentConfig, dict[str, ValidationMessage]]:
    """Normalise create settings and check them like the create wizard."""
    errors: dict[str, ValidationMessage] = {}

    def add(field: str, msgid: str, **params: str | int) -> None:
        errors.setdefault(
            field, ValidationMessage(msgid, tuple(params.items()))
        )

    def parse_text(
        field: str, raw: str | None, max_length: int, *, multiline: bool
    ) -> str | None:
        if raw is None:
            return None
        if multiline:
            raw = raw.replace('\r\n', '\n').replace('\r', '\n')
        text = raw.strip()
        if has_unstorable_character(text):
            add(field, UNSTORABLE_CHARACTER_ERROR)
        elif text and len(raw) > max_length:
            add(field, LENGTH_ERROR, max=max_length, length=len(raw))
        return text

    name = config_input.name.strip()
    if not name:
        add('name', NAME_REQUIRED_ERROR)
    elif has_unstorable_character(name):
        add('name', UNSTORABLE_CHARACTER_ERROR)
    elif len(name) > MAX_NAME_LENGTH:
        add('name', LENGTH_ERROR, max=MAX_NAME_LENGTH, length=len(name))

    raw_category = config_input.category
    category = (
        TournamentCategory.__members__.get(raw_category)
        if isinstance(raw_category, str)
        else None
    )
    if category is None:
        add('category', CATEGORY_INVALID_ERROR)

    game = parse_text(
        'game', config_input.game, MAX_GAME_LENGTH, multiline=False
    )
    description = parse_text(
        'description',
        config_input.description,
        MAX_TEXT_LENGTH,
        multiline=True,
    )
    ruleset = parse_text(
        'ruleset', config_input.ruleset, MAX_TEXT_LENGTH, multiline=True
    )

    start_time = config_input.start_time
    if (
        start_time is not None
        and not tournament_request_domain_service.is_within_service_datetime_bounds(
            start_time
        )
    ):
        add(
            'start_time',
            tournament_request_domain_service.YEAR_RANGE_ERROR_MESSAGE,
        )

    contestant_type = _parse_enum(
        errors,
        'contestant_type',
        ContestantType,
        config_input.contestant_type,
        CONTESTANT_TYPE_INVALID_ERROR,
    )
    game_format = _parse_enum(
        errors,
        'game_format',
        GameFormat,
        config_input.game_format,
        GAME_FORMAT_INVALID_ERROR,
    )
    elimination_mode = _parse_enum(
        errors,
        'elimination_mode',
        EliminationMode,
        config_input.elimination_mode,
        ELIMINATION_MODE_INVALID_ERROR,
    )
    score_ordering = _parse_enum(
        errors,
        'score_ordering',
        ScoreOrdering,
        config_input.score_ordering,
        SCORE_ORDERING_INVALID_ERROR,
    )

    if game_format == GameFormat.HIGHSCORE:
        elimination_mode = EliminationMode.NONE

    count_error = False
    for field, minimum, below, maximum in _COUNT_RULES:
        value = getattr(config_input, field)
        if value is None:
            continue
        if value < minimum:
            errors.setdefault(field, below)
            count_error = True
        elif value > maximum:
            add(field, AT_MOST_ERROR, max=maximum)
            count_error = True

    for min_field, max_field, label in _MIN_MAX_PAIRS:
        min_value = getattr(config_input, min_field)
        max_value = getattr(config_input, max_field)
        if (
            min_value is not None
            and max_value is not None
            and min_value > max_value
        ):
            add(max_field, MIN_ABOVE_MAX_ERROR, other=label, n=min_value)
            count_error = True

    # The domain rules assume counts within their ranges.
    if count_error:
        return Err(errors)

    min_players = config_input.min_players
    max_players = config_input.max_players
    min_teams = config_input.min_teams
    max_teams = config_input.max_teams
    min_players_in_team = config_input.min_players_in_team
    max_players_in_team = config_input.max_players_in_team

    effective_contestant_type = (
        tournament_domain_service.derive_contestant_type(
            contestant_type, max_players_in_team, min_players_in_team
        )
    )
    if effective_contestant_type == ContestantType.SOLO:
        min_teams = None
        max_teams = None
        min_players_in_team = None
        max_players_in_team = None
    elif effective_contestant_type == ContestantType.TEAM:
        min_players = None
        max_players = None

    if game_format != GameFormat.HIGHSCORE:
        score_ordering = None

    playoff, playoff_errors = playoff_config(
        enabled=config_input.playoff_enabled,
        game_format=game_format,
        elimination_mode=elimination_mode,
        group_count=config_input.playoff_group_count,
        qualifiers_per_group=config_input.playoff_qualifiers_per_group,
        qualifier_count=config_input.playoff_qualifier_count,
        elimination_mode_name=config_input.playoff_elimination_mode,
        release_mode_name=config_input.playoff_release_mode,
    )
    for field, message in playoff_errors.items():
        errors.setdefault(field, message)

    point_table = None
    advancement_count = None
    group_size_min = None
    group_size_max = None
    points_carry_to_losers = None
    if game_format == GameFormat.FREE_FOR_ALL:
        ffa_mode = elimination_mode
    elif playoff['playoff_game_format'] == GameFormat.FREE_FOR_ALL:
        ffa_mode = playoff['playoff_elimination_mode']
    else:
        ffa_mode = None
    if (
        game_format == GameFormat.FREE_FOR_ALL
        or playoff['playoff_game_format'] == GameFormat.FREE_FOR_ALL
    ):
        raw_point_table = config_input.point_table
        if isinstance(raw_point_table, str):
            if raw_point_table:
                try:
                    point_table = [
                        int(v.strip())
                        for v in raw_point_table.split(',')
                        if v.strip()
                    ]
                except ValueError:
                    add('point_table', POINT_TABLE_TEXT_ERROR)
        elif raw_point_table:
            point_table = list(raw_point_table) or None
        advancement_count = config_input.advancement_count
        group_size_min = config_input.group_size_min
        group_size_max = config_input.group_size_max
        if ffa_mode == EliminationMode.DOUBLE_ELIMINATION:
            points_carry_to_losers = bool(config_input.points_carry_to_losers)

    settings = tournament_domain_service.TournamentSettings(
        contestant_type=contestant_type,
        game_format=game_format,
        elimination_mode=elimination_mode,
        score_ordering=score_ordering,
        min_players=min_players,
        max_players=max_players,
        min_teams=min_teams,
        max_teams=max_teams,
        min_players_in_team=min_players_in_team,
        max_players_in_team=max_players_in_team,
        point_table=point_table,
        group_size_min=group_size_min,
        group_size_max=group_size_max,
        advancement_count=advancement_count,
        **playoff,
    )
    match tournament_domain_service.validate_tournament_settings(
        settings, require_structure=True
    ):
        case Err(settings_errors):
            for field, message in settings_errors.items():
                errors.setdefault(field, message)

    if errors or category is None:
        return Err(errors)

    return Ok(
        TournamentConfig(
            name=name,
            category=category,
            game=game,
            description=description,
            ruleset=ruleset,
            start_time=start_time,
            settings=settings,
            points_carry_to_losers=points_carry_to_losers,
        )
    )


def has_unstorable_character(value: str) -> bool:
    """Return `True` if the text holds NUL or an unpaired surrogate."""
    return '\x00' in value or any(0xD800 <= ord(c) <= 0xDFFF for c in value)


def playoff_config(
    *,
    enabled: bool,
    game_format: GameFormat | None,
    elimination_mode: EliminationMode | None,
    group_count: int | None,
    qualifiers_per_group: int | None,
    qualifier_count: int | None,
    elimination_mode_name: str | None,
    release_mode_name: str | None,
) -> tuple[dict[str, Any], dict[str, ValidationMessage]]:
    """Return the six playoff kwargs a submission configures.

    All are `None` unless the switch is on and the format has a playoff
    phase, so stale values of a skipped step never reach the service.
    """
    config: dict[str, Any] = dict.fromkeys(PLAYOFF_KWARGS)
    errors: dict[str, ValidationMessage] = {}

    is_round_robin = (
        game_format == GameFormat.ONE_V_ONE
        and elimination_mode == EliminationMode.ROUND_ROBIN
    )
    is_highscore = game_format == GameFormat.HIGHSCORE
    if not enabled or not (is_round_robin or is_highscore):
        return config, errors

    if is_round_robin:
        config['playoff_game_format'] = GameFormat.ONE_V_ONE
        config['playoff_group_count'] = group_count
        config['playoff_qualifiers_per_group'] = qualifiers_per_group
    else:
        config['playoff_game_format'] = GameFormat.FREE_FOR_ALL
        config['playoff_qualifier_count'] = qualifier_count

    config['playoff_elimination_mode'] = _parse_enum(
        errors,
        'playoff_elimination_mode',
        EliminationMode,
        elimination_mode_name,
        ELIMINATION_MODE_INVALID_ERROR,
    )
    config['playoff_release_mode'] = _parse_enum(
        errors,
        'playoff_release_mode',
        PlayoffReleaseMode,
        release_mode_name,
        RELEASE_MODE_INVALID_ERROR,
    )

    return config, errors


def config_input_of(tournament: Tournament) -> TournamentConfigInput:
    """Return the create settings of a tournament, ready to export."""
    start_time = tournament.start_time
    if start_time is not None and start_time.tzinfo is not None:
        start_time = start_time.astimezone(UTC).replace(tzinfo=None)

    return TournamentConfigInput(
        name=tournament.name,
        category=tournament.category.name,
        game=tournament.game,
        description=tournament.description,
        ruleset=tournament.ruleset,
        start_time=start_time,
        contestant_type=_name_of(tournament.contestant_type),
        game_format=_name_of(tournament.game_format),
        elimination_mode=_name_of(tournament.elimination_mode),
        score_ordering=_name_of(tournament.score_ordering),
        min_players=tournament.min_players,
        max_players=tournament.max_players,
        min_teams=tournament.min_teams,
        max_teams=tournament.max_teams,
        min_players_in_team=tournament.min_players_in_team,
        max_players_in_team=tournament.max_players_in_team,
        point_table=(
            list(tournament.point_table) if tournament.point_table else None
        ),
        advancement_count=tournament.advancement_count,
        group_size_min=tournament.group_size_min,
        group_size_max=tournament.group_size_max,
        points_carry_to_losers=bool(tournament.points_carry_to_losers),
        playoff_enabled=tournament.has_playoffs,
        playoff_group_count=tournament.playoff_group_count,
        playoff_qualifiers_per_group=tournament.playoff_qualifiers_per_group,
        playoff_qualifier_count=tournament.playoff_qualifier_count,
        playoff_elimination_mode=_name_of(tournament.playoff_elimination_mode),
        playoff_release_mode=_name_of(tournament.playoff_release_mode),
    )


def _name_of(member: Enum | None) -> str | None:
    return member.name if member is not None else None


def _parse_enum[E: Enum](
    errors: dict[str, ValidationMessage],
    field: str,
    enum_class: type[E],
    raw: str | None,
    invalid_msgid: str,
) -> E | None:
    """Look up an enum member by name; flag an unknown name."""
    if not raw:
        return None

    member = enum_class.__members__.get(raw) if isinstance(raw, str) else None
    if member is None:
        errors.setdefault(field, ValidationMessage(invalid_msgid))
    return member
