import dataclasses
from datetime import datetime, UTC
from typing import NamedTuple, TYPE_CHECKING
from urllib.parse import urlparse
from uuid import UUID

from sqlalchemy.exc import IntegrityError

from byceps.services.party import party_service
from byceps.services.party.models import PartyID
from byceps.services.user.models import UserID
from byceps.util.result import Err, Ok, Result

from . import (
    signals,
    tournament_domain_service,
    tournament_match_service,
    tournament_image_service,
    tournament_operational_service,
    tournament_orga_repository,
    tournament_participant_service,
    tournament_repository,
    tournament_request_repository,
    tournament_request_service,
    tournament_qualification_repository,
    tournament_score_service,
    tournament_seeding_repository,
    tournament_seeding_service,
    tournament_team_service,
)
from .db_error_helpers import extract_constraint_name
from .models.bracket import Bracket
from .events import (
    TournamentCreatedEvent,
    TournamentDeletedEvent,
    TournamentStatusChangedEvent,
    TournamentUpdatedEvent,
)
from .models.contestant_type import ContestantType
from .models.tournament import Tournament, TournamentID
from .models.tournament_match import MatchInvitationID
from .models.tournament_category import TournamentCategory
from .models.tournament_image import TournamentImageID
from .models.score_ordering import ScoreOrdering
from .models.game_format import GameFormat, is_valid_combination
from .models.elimination_mode import EliminationMode
from .models.operational_timing import OperationalClock
from .models.playoff import PlayoffReleaseMode
from .models.tournament_status import TournamentStatus
from .models.validation_message import ValidationMessage
from .tournament_log_service import create_log_entry

if TYPE_CHECKING:
    from .models.tournament_match import TournamentMatch
    from .models.tournament_request import TournamentRequestID


DUPLICATE_SUBMISSION_ERROR = 'This tournament has already been created.'
IMAGE_UNAVAILABLE_ERROR = tournament_image_service.IMAGE_UNAVAILABLE_ERROR
# Same msgid as the domain's place count rule, which has a German entry.
TOO_MANY_PLACES_MSGID = 'At most %(max)s places.'

# Statuses where only cosmetic fields may be edited.
EDIT_LOCKED_STATUSES: frozenset[TournamentStatus] = frozenset({
    TournamentStatus.ONGOING,
    TournamentStatus.PAUSED,
})

PLAYOFF_FIELDS = (
    'playoff_game_format',
    'playoff_elimination_mode',
    'playoff_group_count',
    'playoff_qualifiers_per_group',
    'playoff_qualifier_count',
    'playoff_release_mode',
)
# A highscore tournament plays these in its playoff phase only.
_HIGHSCORE_PLAYOFF_FIELDS = (
    'point_table',
    'group_size_min',
    'group_size_max',
    'advancement_count',
    'points_carry_to_losers',
)
# The playoff switch and the group count shape the phase-1 groups.
_STARTED_PLAYOFF_LOCK = frozenset(
    {'playoff_game_format', 'playoff_group_count'}
)
_STARTED_STATUSES = EDIT_LOCKED_STATUSES | {TournamentStatus.COMPLETED}
PLAYOFF_RELEASED_EDIT_ERROR = (
    'The playoffs are released, so their settings are locked.'
)
PLAYOFF_STARTED_EDIT_ERROR = (
    'Once the tournament has started, the playoffs cannot be switched on '
    'or off and the group count cannot change.'
)


def _playoff_value(name: str, value: object) -> object:
    """Return the value as compared: a missing carry flag is off."""
    if name == 'points_carry_to_losers':
        return bool(value)
    return value


def playoff_fields(tournament: Tournament) -> tuple[str, ...]:
    """Return the fields the playoff rules govern, not the status lock."""
    if (
        tournament.has_playoffs
        and tournament.game_format is GameFormat.HIGHSCORE
    ):
        return PLAYOFF_FIELDS + _HIGHSCORE_PLAYOFF_FIELDS
    return PLAYOFF_FIELDS


def locked_playoff_fields(tournament: Tournament) -> frozenset[str]:
    """Return the playoff fields an edit may not change now.

    All of them once the playoffs are released, in every status. Before
    the release, the playoff switch and the group count once the
    tournament has started; the rest stays editable.
    """
    if tournament.playoff_released_at is not None:
        return frozenset(playoff_fields(tournament))
    if tournament.tournament_status in _STARTED_STATUSES:
        return _STARTED_PLAYOFF_LOCK
    return frozenset()


def _validate_ffa_config(
    game_format: GameFormat | None,
    point_table: list[int] | None,
    group_size_max: int | None,
    contestant_type: ContestantType | None,
    max_teams: int | None,
    group_size_min: int | None,
    *,
    check_table_limits: bool = True,
) -> Result[None, str]:
    """Validate FFA-specific configuration fields.

    Returns ``Ok(None)`` when the format is not FFA or when all
    required FFA fields are present and consistent.

    An update passes ``check_table_limits=False`` for an unchanged
    stored table, so a legacy table above the place and points
    ceilings stays editable.
    """
    if game_format != GameFormat.FREE_FOR_ALL:
        return Ok(None)

    if point_table is None:
        return Err('FFA tournaments require a point_table.')
    if check_table_limits:
        max_places = tournament_domain_service.MAX_POINT_TABLE_PLACES
        if len(point_table) > max_places:
            return Err(TOO_MANY_PLACES_MSGID % {'max': max_places})
        out_of_range = tournament_domain_service.check_point_values(
            point_table
        )
        if out_of_range is not None:
            return Err(out_of_range.msgid % dict(out_of_range.params))
    if group_size_max is None:
        return Err('FFA tournaments require group_size_max.')
    if group_size_min is not None and group_size_min > group_size_max:
        return Err(
            f'group_size_min ({group_size_min}) must not exceed '
            f'group_size_max ({group_size_max}).'
        )
    # FFA+TEAM cross-validation
    if (
        contestant_type == ContestantType.TEAM
        and max_teams is not None
        and group_size_min is not None
        and max_teams < group_size_min
    ):
        return Err(
            'Cannot create FFA team tournament: '
            f'max_teams ({max_teams}) < group_size_min '
            f'({group_size_min}). Impossible to form valid groups.'
        )
    return Ok(None)


class _Unset:
    """Marker type: a playoff kwarg that was not passed."""


_UNSET = _Unset()


def _settings_of(
    tournament: Tournament,
) -> tournament_domain_service.TournamentSettings:
    """Return the settings the domain rules check."""
    return tournament_domain_service.TournamentSettings(
        contestant_type=tournament.contestant_type,
        game_format=tournament.game_format,
        elimination_mode=tournament.elimination_mode,
        score_ordering=tournament.score_ordering,
        min_players=tournament.min_players,
        max_players=tournament.max_players,
        min_teams=tournament.min_teams,
        max_teams=tournament.max_teams,
        min_players_in_team=tournament.min_players_in_team,
        max_players_in_team=tournament.max_players_in_team,
        point_table=tournament.point_table,
        group_size_min=tournament.group_size_min,
        group_size_max=tournament.group_size_max,
        advancement_count=tournament.advancement_count,
        playoff_game_format=tournament.playoff_game_format,
        playoff_elimination_mode=tournament.playoff_elimination_mode,
        playoff_group_count=tournament.playoff_group_count,
        playoff_qualifiers_per_group=tournament.playoff_qualifiers_per_group,
        playoff_qualifier_count=tournament.playoff_qualifier_count,
        playoff_release_mode=tournament.playoff_release_mode,
    )


def _cut_editable(tournament: Tournament) -> bool:
    """Tell whether an update may still change the cut."""
    if 'advancement_count' in playoff_fields(tournament):
        return 'advancement_count' not in locked_playoff_fields(tournament)
    return tournament.tournament_status not in EDIT_LOCKED_STATUSES


def _validate_playoff_config(
    tournament: Tournament,
) -> Result[None, ValidationMessage]:
    """Return the message unformatted, for the view to format."""
    settings = _settings_of(tournament)
    match tournament_domain_service.validate_playoff_settings(settings):
        case Err(errors):
            return Err(next(iter(errors.values())))
    return Ok(None)


def _validate_image_url(image_url: str | None) -> Result[None, str]:
    """Validate image URL to prevent XSS/SSRF attacks."""
    if image_url is None or image_url == '':
        return Ok(None)

    try:
        parsed = urlparse(image_url)
        if parsed.scheme not in ('http', 'https'):
            return Err('Image URL must use http or https scheme.')
        if not parsed.netloc:
            return Err('Image URL must have a valid domain.')
    except Exception:
        return Err('Invalid image URL format.')

    return Ok(None)


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
    position: int | None = None,
    category: TournamentCategory | None = None,
    created_from_request_id: 'TournamentRequestID | None' = None,
    initiator_id: UserID | None = None,
    image_id: TournamentImageID | None = None,
    image_alt_text: str | None = None,
    creation_token: UUID | None = None,
) -> Result[tuple[Tournament, TournamentCreatedEvent], str | ValidationMessage]:
    """Create a tournament.

    SECURITY NOTE: Authorization must be checked at blueprint layer before
    calling this function (requires 'lan_tournament.create' permission).

    `initiator_id` is required when `created_from_request_id` is set:
    it is who the request-link audit entry is filed under. Passing
    one without the other is a caller bug, not a user-facing error, so
    it raises rather than returning `Err`.
    """
    if category is None:
        category = (
            TournamentCategory.USER_ORGANIZED
            if created_from_request_id is not None
            else TournamentCategory.MAIN
        )
    elif not isinstance(category, TournamentCategory):
        return Err('Please choose a valid tournament category.')

    if created_from_request_id is not None:
        if initiator_id is None:
            raise ValueError(
                'initiator_id is required when created_from_request_id '
                'is set.'
            )

    # Never store a NULL contestant type: derive it from team size
    # before validation, so the FFA/team cross-check below sees it too.
    contestant_type = tournament_domain_service.derive_contestant_type(
        contestant_type, max_players_in_team, min_players_in_team
    )

    # Auto-assign position if not explicitly provided.
    if position is None:
        position = tournament_repository.get_max_position_for_party(party_id) + 1

    # Validate name length
    if len(name.strip()) > 80:
        return Err('Tournament name must not exceed 80 characters.')

    if image_id is not None:
        # The stored image overrides any posted URL.
        party = party_service.get_party(party_id)
        image = tournament_image_service.find_attachable_image(
            image_id, party
        )
        if image is None:
            return Err(IMAGE_UNAVAILABLE_ERROR)
        image_url = tournament_image_service.get_image_url_path(image)
    else:
        # Validate image URL
        validation_result = _validate_image_url(image_url)
        if validation_result.is_err():
            return Err(validation_result.unwrap_err())

    # Validate game_format + elimination_mode combination
    if game_format is not None and elimination_mode is not None:
        if not is_valid_combination(game_format, elimination_mode):
            return Err(
                f'Invalid combination: {game_format.name} + '
                f'{elimination_mode.name}.'
            )

    # FFA-specific validation
    ffa_result = _validate_ffa_config(
        game_format, point_table, group_size_max,
        contestant_type, max_teams, group_size_min,
    )
    if ffa_result.is_err():
        return Err(ffa_result.unwrap_err())

    tournament, event = tournament_domain_service.create_tournament(
        party_id,
        name,
        game=game,
        description=description,
        image_url=image_url,
        ruleset=ruleset,
        start_time=start_time,
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

    playoff_result = _validate_playoff_config(tournament)
    if playoff_result.is_err():
        return Err(playoff_result.unwrap_err())

    if tournament_domain_service.ffa_cut_missing(_settings_of(tournament)):
        return Err(tournament_domain_service.FFA_CUT_REQUIRED_MSGID)

    try:
        tournament_repository.create_tournament(
            tournament, commit=created_from_request_id is None
        )
    except IntegrityError as e:
        tournament_repository.rollback_session()
        constraint_name = extract_constraint_name(e)
        if constraint_name == 'uq_lan_tournaments_created_from_request_id':
            return Err(
                'A tournament has already been created from this request.'
            )
        if constraint_name == 'uq_lan_tournaments_creation_token':
            return Err(DUPLICATE_SUBMISSION_ERROR)
        if constraint_name == 'fk_lan_tournaments_image_id':
            return Err(IMAGE_UNAVAILABLE_ERROR)
        raise
    except Exception:
        # A DataError/OperationalError from the flush must not leave
        # the session pending-rollback for the caller.
        tournament_repository.rollback_session()
        raise

    if created_from_request_id is not None:
        if initiator_id is None:
            # Structurally unreachable -- the guard at the top of this
            # function already refused this combination. Repeated here
            # (as a `raise`, not an `assert`, which ruff's bandit
            # rules flag in non-test code) so the type checker can
            # narrow `initiator_id` for the call below.
            raise ValueError(
                'initiator_id is required when created_from_request_id '
                'is set.'
            )

        # The tournament row above is flushed but not yet committed:
        # lock and re-check the request, then stage its link update
        # and audit entry in the same transaction, so the two either
        # both land in the one commit below or neither does (an
        # unrecoverable error here must not leave a committed
        # tournament pointing at a request that never got linked).
        try:
            link_result = (
                tournament_request_service.link_created_tournament_flush(
                    created_from_request_id,
                    party_id,
                    tournament.id,
                    initiator_id,
                )
            )
        except Exception:
            tournament_repository.rollback_session()
            raise

        if link_result.is_err():
            tournament_repository.rollback_session()
            return Err(link_result.unwrap_err())

        try:
            tournament_repository.commit_session()
        except Exception:
            tournament_repository.rollback_session()
            raise

    signals.tournament_created.send(None, event=event)

    return Ok((tournament, event))


def update_tournament(
    tournament_id: TournamentID,
    *,
    name: str,
    category: TournamentCategory | None = None,
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
    game_format: GameFormat | None = None,
    elimination_mode: EliminationMode | None = None,
    score_ordering: ScoreOrdering | None = None,
    point_table: list[int] | None = None,
    advancement_count: int | None = None,
    group_size_min: int | None = None,
    group_size_max: int | None = None,
    points_carry_to_losers: bool | None = None,
    playoff_game_format: GameFormat | None | _Unset = _UNSET,
    playoff_elimination_mode: EliminationMode | None | _Unset = _UNSET,
    playoff_group_count: int | None | _Unset = _UNSET,
    playoff_qualifiers_per_group: int | None | _Unset = _UNSET,
    playoff_qualifier_count: int | None | _Unset = _UNSET,
    playoff_release_mode: PlayoffReleaseMode | None | _Unset = _UNSET,
    initiator_id: UserID | None = None,
) -> Result[Tournament, str | ValidationMessage]:
    """Update a tournament.

    SECURITY NOTE: Authorization must be checked at blueprint layer before
    calling this function (requires 'lan_tournament.update' permission).

    Note: tournament_status is not accepted here.  Status
    changes must go through ``change_status`` to enforce the
    state machine.

    A playoff kwarg that is not passed keeps the stored value; pass
    `None` to clear it. The release state is never a kwarg here: the
    targeted repository writers own it.

    The playoff fields follow `locked_playoff_fields`, not the status
    lock. With an `initiator_id`, a changed playoff field of an ongoing
    tournament tries the automatic release, since a new cut or release
    mode can make it due.
    """
    if category is not None and not isinstance(category, TournamentCategory):
        return Err('Please choose a valid tournament category.')

    # Never store a NULL contestant type: derive it from team size
    # before validation, so the FFA/team cross-check below sees it too.
    contestant_type = tournament_domain_service.derive_contestant_type(
        contestant_type, max_players_in_team, min_players_in_team
    )

    # Validate name length
    if len(name.strip()) > 80:
        return Err('Tournament name must not exceed 80 characters.')

    # Read under the lock: a stale snapshot would hide a concurrent start
    # from the structural lock below.
    tournament_repository.lock_tournament_for_update(tournament_id)
    tournament = tournament_repository.get_tournament(tournament_id)
    if category is None:
        category = tournament.category

    # An unchanged URL (e.g. an uploaded image's relative served path)
    # stays valid without re-validation.
    image_url_changed = (image_url or None) != (tournament.image_url or None)
    if image_url_changed:
        validation_result = _validate_image_url(image_url)
        if validation_result.is_err():
            tournament_repository.rollback_session()
            return Err(validation_result.unwrap_err())

    # Validate game_format + elimination_mode combination
    if game_format is not None and elimination_mode is not None:
        if not is_valid_combination(game_format, elimination_mode):
            tournament_repository.rollback_session()
            return Err(
                f'Invalid combination: {game_format.name} + '
                f'{elimination_mode.name}.'
            )

    # FFA-specific validation
    ffa_result = _validate_ffa_config(
        game_format, point_table, group_size_max,
        contestant_type, max_teams, group_size_min,
        check_table_limits=point_table != tournament.point_table,
    )
    if ffa_result.is_err():
        tournament_repository.rollback_session()
        return Err(ffa_result.unwrap_err())

    if isinstance(playoff_game_format, _Unset):
        playoff_game_format = tournament.playoff_game_format
    if isinstance(playoff_elimination_mode, _Unset):
        playoff_elimination_mode = tournament.playoff_elimination_mode
    if isinstance(playoff_group_count, _Unset):
        playoff_group_count = tournament.playoff_group_count
    if isinstance(playoff_qualifiers_per_group, _Unset):
        playoff_qualifiers_per_group = tournament.playoff_qualifiers_per_group
    if isinstance(playoff_qualifier_count, _Unset):
        playoff_qualifier_count = tournament.playoff_qualifier_count
    if isinstance(playoff_release_mode, _Unset):
        playoff_release_mode = tournament.playoff_release_mode

    # Validate the playoff fields against the incoming structure.
    playoff_result = _validate_playoff_config(
        dataclasses.replace(
            tournament,
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
            playoff_game_format=playoff_game_format,
            playoff_elimination_mode=playoff_elimination_mode,
            playoff_group_count=playoff_group_count,
            playoff_qualifiers_per_group=playoff_qualifiers_per_group,
            playoff_qualifier_count=playoff_qualifier_count,
            playoff_release_mode=playoff_release_mode,
        )
    )
    if playoff_result.is_err():
        tournament_repository.rollback_session()
        return Err(playoff_result.unwrap_err())

    # Reject structural changes while the tournament is in play.
    if tournament.tournament_status in EDIT_LOCKED_STATUSES:
        locked_changes: list[str] = []
        if category != tournament.category:
            locked_changes.append('category')
        if name != tournament.name:
            locked_changes.append('name')
        if game != tournament.game:
            locked_changes.append('game')
        if start_time != tournament.start_time:
            locked_changes.append('start_time')
        # Compare derived-vs-derived: a legacy NULL `contestant_type`
        # whose team size derives to the same effective type as the
        # incoming (also derived) one is not a change.
        stored_contestant_type = (
            tournament_domain_service.derive_contestant_type(
                tournament.contestant_type,
                tournament.max_players_in_team,
                tournament.min_players_in_team,
            )
        )
        if contestant_type != stored_contestant_type:
            locked_changes.append('contestant_type')
        if game_format != tournament.game_format:
            locked_changes.append('game_format')
        if elimination_mode != tournament.elimination_mode:
            locked_changes.append('elimination_mode')
        if score_ordering != tournament.score_ordering:
            locked_changes.append('score_ordering')
        if min_players != tournament.min_players:
            locked_changes.append('min_players')
        if max_players != tournament.max_players:
            locked_changes.append('max_players')
        if min_teams != tournament.min_teams:
            locked_changes.append('min_teams')
        if max_teams != tournament.max_teams:
            locked_changes.append('max_teams')
        if min_players_in_team != tournament.min_players_in_team:
            locked_changes.append('min_players_in_team')
        if max_players_in_team != tournament.max_players_in_team:
            locked_changes.append('max_players_in_team')
        if point_table != tournament.point_table:
            locked_changes.append('point_table')
        if advancement_count != tournament.advancement_count:
            locked_changes.append('advancement_count')
        if group_size_min != tournament.group_size_min:
            locked_changes.append('group_size_min')
        if group_size_max != tournament.group_size_max:
            locked_changes.append('group_size_max')
        if points_carry_to_losers != tournament.points_carry_to_losers:
            locked_changes.append('points_carry_to_losers')
        governed = playoff_fields(tournament)
        locked_changes = [n for n in locked_changes if n not in governed]
        if locked_changes:
            fields_str = ', '.join(locked_changes)
            tournament_repository.rollback_session()
            return Err(
                f'Tournament is {tournament.tournament_status.name.lower()}. '
                f'Only description, image, and ruleset can be changed. '
                f'Attempted to change: {fields_str}.'
            )

    incoming = {
        'playoff_game_format': playoff_game_format,
        'playoff_elimination_mode': playoff_elimination_mode,
        'playoff_group_count': playoff_group_count,
        'playoff_qualifiers_per_group': playoff_qualifiers_per_group,
        'playoff_qualifier_count': playoff_qualifier_count,
        'playoff_release_mode': playoff_release_mode,
        'point_table': point_table,
        'group_size_min': group_size_min,
        'group_size_max': group_size_max,
        'advancement_count': advancement_count,
        'points_carry_to_losers': points_carry_to_losers,
    }
    changed_playoff_fields = {
        name
        for name in playoff_fields(tournament)
        if _playoff_value(name, incoming[name])
        != _playoff_value(name, getattr(tournament, name))
    }
    if changed_playoff_fields & locked_playoff_fields(tournament):
        tournament_repository.rollback_session()
        if tournament.playoff_released_at is not None:
            return Err(PLAYOFF_RELEASED_EDIT_ERROR)
        return Err(PLAYOFF_STARTED_EDIT_ERROR)

    updated = dataclasses.replace(
        tournament,
        name=name,
        category=category,
        game=game,
        description=description,
        image_url=image_url,
        image_id=None if image_url_changed else tournament.image_id,
        image_alt_text=(
            None
            if image_url_changed and tournament.image_id is not None
            else tournament.image_alt_text
        ),
        ruleset=ruleset,
        start_time=start_time,
        updated_at=datetime.now(UTC),
        min_players=min_players,
        max_players=max_players,
        min_teams=min_teams,
        max_teams=max_teams,
        min_players_in_team=min_players_in_team,
        max_players_in_team=max_players_in_team,
        contestant_type=contestant_type,
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
    )

    # A legacy row with an empty cut that is locked still saves other edits.
    if tournament_domain_service.ffa_cut_missing(_settings_of(updated)) and (
        tournament.advancement_count is not None or _cut_editable(tournament)
    ):
        tournament_repository.rollback_session()
        return Err(tournament_domain_service.FFA_CUT_REQUIRED_MSGID)

    try:
        tournament_repository.update_tournament(updated)
    except Exception:
        tournament_repository.rollback_session()
        raise

    event = TournamentUpdatedEvent(
        occurred_at=datetime.now(UTC),
        initiator=None,
        tournament_id=tournament_id,
    )
    signals.tournament_updated.send(None, event=event)

    # A changed cut or release mode can make the release due.
    if (
        initiator_id is not None
        and changed_playoff_fields
        and updated.has_playoffs
        and updated.tournament_status is TournamentStatus.ONGOING
    ):
        from . import tournament_qualification_service

        tournament_qualification_service.auto_release_after_commit(
            tournament_id, triggered_by=initiator_id
        )

    return Ok(updated)


def find_tournament_by_creation_token(
    creation_token: UUID,
) -> Tournament | None:
    """Return the tournament created with the token, if any."""
    return tournament_repository.find_tournament_by_creation_token(
        creation_token
    )


def delete_tournament(
    tournament_id: TournamentID,
    initiator_id: UserID | None = None,
) -> None:
    """Delete a tournament and all dependent entities.

    SECURITY NOTE: Authorization must be checked at blueprint layer
    before calling this function (requires
    'lan_tournament.delete' permission).

    Log entries are kept; a `tournament-deleted` entry records the
    tournament's name, party, game and status.

    CASCADE HANDLING: Deletes all dependent entities in correct
    order:
    1. Score submissions (FK to participants/teams)
    2. Match comments
    3. Match contestants
    4. Matches
    5. Winner references (FK back to teams/participants)
    6. Participants
    7. Teams
    8. Orga assignments, seeding drafts and qualification decisions
    9. Tournament request link (`fk_lan_tournament_requests_created_
       tournament_id` carries no ON DELETE; a request that produced
       this tournament has its `created_tournament_id` cleared here,
       and the clearing recorded in its own history, before the
       tournament row that FK points at is removed)
    10. Tournament itself
    """
    tournament_repository.lock_tournament_for_update(tournament_id)

    tournament = tournament_repository.get_tournament(tournament_id, fresh=True)

    # Delete in dependency order (children first, then parent).
    # All repo calls use commit=False so the entire cascade is a
    # single atomic transaction committed once at the end.
    # Wrapped in try/except to rollback on partial flush failure,
    # preventing session poisoning if a caller catches the exception.
    try:
        create_log_entry(
            'tournament-deleted',
            tournament_id,
            initiator_id,
            data={
                'name': tournament.name,
                'party_id': str(tournament.party_id),
                'game': tournament.game,
                # Log the name, as the enum values are positional ints.
                'tournament_status': (
                    tournament.tournament_status.name
                    if tournament.tournament_status is not None
                    else None
                ),
            },
            commit=False,
        )

        tournament_repository.delete_submissions_for_tournament(
            tournament_id, commit=False
        )
        tournament_repository.delete_comments_for_tournament(
            tournament_id, commit=False
        )
        tournament_repository.delete_contestants_for_tournament(
            tournament_id, commit=False
        )
        tournament_repository.delete_matches_for_tournament(
            tournament_id, commit=False
        )
        tournament_repository.clear_winner_for_tournament(
            tournament_id, commit=False
        )
        tournament_repository.delete_participants_for_tournament(
            tournament_id, commit=False
        )
        tournament_repository.delete_teams_for_tournament(
            tournament_id, commit=False
        )
        tournament_orga_repository.delete_orgas_for_tournament(
            tournament_id, commit=False
        )
        tournament_seeding_repository.delete_seedings_for_tournament(
            tournament_id
        )
        tournament_qualification_repository.delete_decisions_for_tournament(
            tournament_id
        )

        # Clear the reverse link before the row it points at is
        # deleted -- migration 016's FK carries no ON DELETE (by
        # design: the linkage is "removed explicitly at the
        # application service layer"). Record the clearing in the
        # request's own history so its timeline shows the tournament
        # was deleted, not just silently unlinked.
        unlinked_requests = (
            tournament_request_repository.unlink_created_tournament_flush(
                tournament_id
            )
        )
        for request in unlinked_requests:
            create_log_entry(
                'tournament-request-tournament-deleted',
                TournamentID(request.id),
                initiator_id,
                data={
                    'tournament_id': str(tournament_id),
                    'tournament_name': tournament.name,
                },
                commit=False,
            )

        tournament_repository.delete_tournament(tournament_id, commit=False)

        tournament_repository.commit_session()
    except Exception:
        tournament_repository.rollback_session()
        raise

    event = TournamentDeletedEvent(
        occurred_at=datetime.now(UTC),
        initiator=None,
        tournament_id=tournament_id,
    )
    signals.tournament_deleted.send(None, event=event)


def get_tournament(
    tournament_id: TournamentID,
) -> Tournament:
    """Return the tournament with that ID."""
    return tournament_repository.get_tournament(tournament_id)


def find_tournament(
    tournament_id: TournamentID,
) -> Tournament | None:
    """Return the tournament, or `None` if not found."""
    return tournament_repository.find_tournament(tournament_id)


def reorder_tournaments(
    party_id: PartyID, tournament_ids: list[str]
) -> None:
    """Reorder tournaments by updating positions to match the given order.

    Validates that every supplied tournament ID belongs to the given
    party, that the list is complete (covers all tournaments), and
    contains no duplicates.  Raises ``ValueError`` on any violation.
    """
    party_tournaments = tournament_repository.get_tournaments_for_party(
        party_id
    )
    valid_ids = {str(t.id) for t in party_tournaments}

    if len(tournament_ids) != len(set(tournament_ids)):
        raise ValueError('Duplicate tournament IDs in reorder list')

    if set(tournament_ids) != valid_ids:
        raise ValueError(
            'Tournament ID list must contain exactly all tournaments '
            f'for party {party_id}'
        )

    try:
        tournament_repository.reorder_tournaments(tournament_ids)
    except Exception:
        tournament_repository.rollback_session()
        raise


def get_tournaments_for_party(
    party_id: PartyID,
) -> list[Tournament]:
    """Return all tournaments for that party."""
    return tournament_repository.get_tournaments_for_party(party_id)


def get_participant_count(
    tournament_id: TournamentID,
) -> int:
    """Return the number of participants."""
    return tournament_repository.get_participant_count(tournament_id)


def get_participant_counts_for_tournaments(
    tournament_ids: list[TournamentID],
) -> dict[TournamentID, int]:
    """Return participant counts keyed by tournament ID."""
    return tournament_repository.get_participant_counts_for_tournaments(
        tournament_ids
    )


def _ffa_round_zero_stall(tournament: Tournament) -> bool:
    """Tell whether the generated first FFA round runs into a dead end."""
    if tournament.game_format is not GameFormat.FREE_FOR_ALL:
        return False
    matches = [
        m
        for m in tournament_repository.get_matches_for_tournament(tournament.id)
        if m.phase == 1 and m.round == 0 and m.bracket is not Bracket.LOSERS
    ]
    if not matches:
        return False
    contestants = tournament_repository.get_contestants_for_matches(
        [m.id for m in matches]
    )
    count = sum(len(members) for members in contestants.values())
    return tournament_match_service.ffa_stall_for(tournament, count) is not None


START_CONFIRM_REQUIRED_ERROR = (
    'Confirm that the generated layout is used before starting.'
)


def _start_confirmation_refusal(
    tournament_id: TournamentID, confirmed: bool
) -> str | None:
    """Return the msgid refusing an unconfirmed start of a changed board."""
    if confirmed:
        return None
    generation = tournament_seeding_service.peek_initial_generation_status(
        tournament_id
    )
    if generation is tournament_seeding_service.GenerationStatus.DIFFERS:
        return START_CONFIRM_REQUIRED_ERROR
    return None


def _reconcile_lifecycle_invitations_flush(
    tournament_id: TournamentID, status: TournamentStatus, *,
    occurred_at: datetime,
) -> Result[tuple[MatchInvitationID, ...], str]:
    """Reconcile work under the owning lifecycle transaction, without effects.

    Terminal suppression must remain irreversible on an authorized reopen. A
    temporary pause uses the storage reconciliation contract so permanent
    failures/retry delays and readiness holds survive. Durable intent/effect
    collection at all writer boundaries remains the owner-intent integration.
    """
    from . import tournament_invitation_service

    matches = tournament_repository.get_matches_for_tournament_ordered_fresh(
        tournament_id
    )
    tournament_repository.lock_matches_for_update([match.id for match in matches])
    pending: set[MatchInvitationID] = set()
    for match in sorted(matches, key=lambda match: str(match.id)):
        if status in {TournamentStatus.COMPLETED, TournamentStatus.CANCELLED}:
            tournament_repository.suppress_match_invitations_flush(
                match.id, reason='tournament_terminal'
            )
        else:
            result = tournament_invitation_service.reconcile_match_invitations_flush(
                match.id, occurred_at=occurred_at
            )
            if result.is_err():
                return Err(result.unwrap_err())
            pending.update(result.unwrap())
    return Ok(tuple(sorted(pending, key=str)))


def change_status(
    tournament_id: TournamentID,
    new_status: TournamentStatus,
    initiator_id: UserID | None = None,
    *,
    confirm_generated_layout: bool = False,
    allow_completed_reopen: bool = False,
) -> Result[tuple[Tournament, TournamentStatusChangedEvent], str]:
    """Change the tournament status.

    Completed reopening requires a trusted caller's explicit opt-in.
    A plain round robin settled while it was not running completes on
    the change into ONGOING; the returned tournament still says ONGOING
    then.
    """
    tournament_repository.lock_tournament_for_update(tournament_id)
    tournament = tournament_repository.get_tournament(tournament_id, fresh=True)

    if (
        tournament.tournament_status == TournamentStatus.COMPLETED
        and new_status != TournamentStatus.COMPLETED
        and not allow_completed_reopen
    ):
        tournament_repository.rollback_session()
        return Err(
            'A completed tournament can only be reopened by an administrator.'
        )

    # Validate state machine transition first
    result = tournament_domain_service.change_tournament_status(
        tournament, new_status
    )
    if result.is_err():
        tournament_repository.rollback_session()
        return Err(result.unwrap_err())

    # Validate the bracket on a start, not on a resume from `PAUSED` or
    # a reopen from `COMPLETED`: play has changed the bracket by then.
    #
    # Stated as what a start IS, not as the states it is not, so that
    # adding another edge into ONGOING cannot silently opt that edge
    # into this validation. `None` is in the set because a tournament
    # that has never carried a status is also starting here -- the
    # old "anything but PAUSED" spelling covered that case, and
    # dropping it would have quietly relaxed the gate.
    is_start = new_status == TournamentStatus.ONGOING and (
        tournament.tournament_status
        in (None, TournamentStatus.REGISTRATION_CLOSED)
    )
    if is_start:
        violations: list[str] = []
        has_board = bool(
            tournament.game_format
            and (
                tournament.game_format.requires_bracket_generation
                or tournament.game_format.uses_placements
            )
        )
        if has_board:
            violations = tournament_match_service.validate_bracket_for_start(
                tournament_id, tournament=tournament
            )
        # A bracket of another structure fails the bracket check too; the
        # seeding reason names the way out. Without matches there is no
        # generation to compare.
        seeding_violations: list[str] = []
        if violations != ['no matches generated']:
            seeding_violations = tournament_seeding_service.start_violations(
                tournament_id
            )
        if not violations or seeding_violations == [
            tournament_seeding_service.ERR_STRUCTURE_CHANGED_AFTER_GENERATION
        ]:
            violations = seeding_violations
        if violations:
            tournament_repository.rollback_session()

            # Only these violations have a catalogue entry.
            if violations == ['no matches generated']:
                return Err(
                    'Cannot start tournament without generated '
                    'brackets. Generate brackets first.'
                )
            if len(violations) == 1 and violations[0] in (
                tournament_seeding_service.ERR_ROSTER_CHANGED,
                tournament_seeding_service.ERR_STRUCTURE_CHANGED_AFTER_GENERATION,
            ):
                return Err(violations[0])
            return Err('Cannot start tournament: ' + '; '.join(violations))
        if has_board:
            refusal = _start_confirmation_refusal(
                tournament_id, confirm_generated_layout
            )
            if refusal is not None:
                tournament_repository.rollback_session()
                return Err(refusal)
        # The cut or the minimum may have changed after the generation.
        if _ffa_round_zero_stall(tournament):
            tournament_repository.rollback_session()
            return Err(tournament_match_service.FFA_STALLS_ERROR)

    (event,) = result.unwrap()

    updated = dataclasses.replace(tournament, tournament_status=new_status)

    # Leaving COMPLETED means the recorded winner is no longer a
    # result -- the tournament is being played again. Clearing it here
    # rather than in the reopen route covers every authorized way out
    # of the status, and mirrors what the retraction cascade in
    # _unconfirm_match_impl already does when it reverts a completion.
    # Flush only: commit_session below owns the commit, so a failure
    # before it discards this with it.
    if (
        tournament.tournament_status == TournamentStatus.COMPLETED
        and new_status != TournamentStatus.COMPLETED
    ):
        cleared = tournament_repository.set_tournament_winner(
            tournament_id,
            winner_team_id=None,
            winner_participant_id=None,
        )
        if cleared.is_err():
            tournament_repository.rollback_session()
            return Err(cleared.unwrap_err())
        updated = dataclasses.replace(
            updated, winner_team_id=None, winner_participant_id=None
        )

    try:
        persisted = _persist_status_change_flush(
            tournament, updated, event, initiator_id
        )
        if persisted.is_err():
            tournament_repository.rollback_session()
            return Err(persisted.unwrap_err())
        tournament_repository.commit_session()
    except Exception:
        tournament_repository.rollback_session()
        raise

    from . import tournament_readiness_service

    change = persisted.unwrap()
    try:
        _dispatch_status_change_effects(tournament, event, initiator_id)
    finally:
        tournament_readiness_service.dispatch_pending_invitations(
            change.pending_invitation_ids,
        )
    return Ok((_with_clock(updated, change.clock), event))


class _PersistedStatusChange(NamedTuple):
    """What a flushed status change leaves for its owner to hand on."""

    pending_invitation_ids: tuple[MatchInvitationID, ...]
    clock: OperationalClock | None


def _with_clock(
    tournament: Tournament, clock: OperationalClock | None
) -> Tournament:
    """Return the tournament with the clock the status change persisted."""
    if clock is None:
        return tournament

    return dataclasses.replace(
        tournament,
        operational_clock_elapsed_us=clock.elapsed_us,
        operational_clock_running_since=clock.running_since,
        operational_clock_activated_at=clock.activated_at,
    )


def _persist_status_change_flush(
    tournament: Tournament,
    updated: Tournament,
    event: TournamentStatusChangedEvent,
    initiator_id: UserID | None,
) -> Result[_PersistedStatusChange, str]:
    """Persist status/work/audit; the owning caller commits and emits effects."""
    tournament_id = tournament.id
    new_status = updated.tournament_status
    # The caller commits this entry together with status and work.
    create_log_entry(
        'tournament-status-changed',
        tournament_id,
        initiator_id,
        data={
            'old_status': (
                tournament.tournament_status.name
                if tournament.tournament_status is not None
                else None
            ),
            'new_status': new_status.name,
        },
        commit=False,
    )

    # Status-only write (flush): `update_tournament` is a full-row
    # writer and would also persist `updated.contestant_type`, which a
    # legacy NULL row's loading derives in memory from team size. Only
    # the explicit edit-form save in `update_tournament` above may
    # persist that derived value.
    status_result = tournament_repository.set_tournament_status_flush(
        tournament_id, new_status
    )
    if status_result.is_err():
        return Err(status_result.unwrap_err())
    clock_edge = status_result.unwrap()
    clock = clock_edge.clock if clock_edge is not None else None

    # A pause freezes the clock and keeps every open episode as it is.
    if clock_edge is not None and new_status is not TournamentStatus.PAUSED:
        reconciled = tournament_operational_service.reconcile_due_matches_flush(
            tournament_id, occurred_at=clock_edge.at
        )
        if reconciled.is_err():
            return Err(reconciled.unwrap_err())

    if new_status in {
        TournamentStatus.ONGOING, TournamentStatus.PAUSED,
        TournamentStatus.COMPLETED, TournamentStatus.CANCELLED,
    }:
        work_result = _reconcile_lifecycle_invitations_flush(
            tournament_id, new_status, occurred_at=event.occurred_at
        )
        if work_result.is_err():
            return Err(work_result.unwrap_err())
        return Ok(_PersistedStatusChange(work_result.unwrap(), clock))

    return Ok(_PersistedStatusChange((), clock))


def _dispatch_status_change_effects(
    tournament: Tournament,
    event: TournamentStatusChangedEvent,
    initiator_id: UserID | None,
) -> None:
    """Existing lifecycle effects, strictly after the owning commit."""
    tournament_id = tournament.id
    new_status = event.new_status
    signals.tournament_status_changed.send(None, event=event)

    # A plain round robin completes only while ONGOING, so one settled
    # while paused or before the start completes now. Not on a reopen:
    # that would undo it at once.
    if (
        new_status == TournamentStatus.ONGOING
        and tournament.tournament_status != TournamentStatus.COMPLETED
        and tournament_match_service.is_plain_round_robin(tournament)
    ):
        tournament_match_service.complete_settled_plain_round_robin(
            tournament_id
        )

    # Only an ONGOING tournament releases, so a qualification that
    # became ready while PAUSED is due now. A refusal leaves things as
    # they are: the qualification panel shows why.
    if (
        new_status == TournamentStatus.ONGOING
        and tournament.has_playoffs
        and initiator_id is not None
    ):
        from . import tournament_qualification_service

        tournament_qualification_service.auto_release_after_commit(
            tournament_id, triggered_by=initiator_id
        )


def start_tournament(
    tournament_id: TournamentID,
    initiator_id: UserID | None = None,
) -> Result[tuple[Tournament, TournamentStatusChangedEvent], str]:
    """Start a tournament."""
    return change_status(tournament_id, TournamentStatus.ONGOING, initiator_id)


def pause_tournament(
    tournament_id: TournamentID,
    initiator_id: UserID | None = None,
) -> Result[tuple[Tournament, TournamentStatusChangedEvent], str]:
    """Pause a tournament."""
    return change_status(tournament_id, TournamentStatus.PAUSED, initiator_id)


def resume_tournament(
    tournament_id: TournamentID,
    initiator_id: UserID | None = None,
) -> Result[tuple[Tournament, TournamentStatusChangedEvent], str]:
    """Resume a paused tournament."""
    return change_status(tournament_id, TournamentStatus.ONGOING, initiator_id)


def end_tournament(
    tournament_id: TournamentID,
    initiator_id: UserID | None = None,
) -> Result[tuple[Tournament, TournamentStatusChangedEvent], str]:
    """End a tournament."""
    return change_status(
        tournament_id, TournamentStatus.COMPLETED, initiator_id
    )


def resolve_winner_display_name(tournament: Tournament) -> str | None:
    """Return a human-readable winner name for a completed tournament,
    or ``None`` if no winner is recorded or the tournament is not
    completed.
    """
    if tournament.tournament_status != TournamentStatus.COMPLETED:
        return None

    if tournament.winner_team_id:
        team = tournament_team_service.find_team(tournament.winner_team_id)
        if team is not None:
            return team.name
    elif tournament.winner_participant_id:
        from byceps.services.user import user_service

        participant = tournament_participant_service.find_participant(
            tournament.winner_participant_id
        )
        if participant is not None:
            user = user_service.get_user(participant.user_id)
            return user.screen_name

    return None


def resolve_podium_display_names(
    tournament: Tournament,
) -> dict[str, str | None]:
    """Return runner-up and bronze display names for a completed tournament.

    Returns a dict with keys ``'runner_up'`` and ``'bronze'``.  Values
    are ``None`` when the tournament is not completed or the position
    cannot be determined.

    The champion is intentionally **not** included — callers should use
    :func:`resolve_winner_display_name` for that to avoid redundant
    look-ups.
    """
    empty: dict[str, str | None] = {
        'runner_up': None,
        'bronze': None,
    }

    if tournament.tournament_status != TournamentStatus.COMPLETED:
        return empty

    # A playoff tournament is decided by phase 2 alone.
    phase = 2 if tournament.has_playoffs else None
    gf = tournament_domain_service.game_format_for_phase(
        tournament, phase or 1
    )
    em = tournament_domain_service.elimination_mode_for_phase(
        tournament, phase or 1
    )
    if gf == GameFormat.ONE_V_ONE and em in (
        EliminationMode.SINGLE_ELIMINATION,
        EliminationMode.DOUBLE_ELIMINATION,
    ):
        runner_up, bronze = _resolve_bracket_podium(tournament, phase=phase)
    elif em == EliminationMode.ROUND_ROBIN:
        runner_up, bronze = _resolve_rr_podium(tournament)
    elif gf == GameFormat.HIGHSCORE:
        runner_up, bronze = _resolve_hs_podium(tournament)
    elif gf == GameFormat.FREE_FOR_ALL:
        runner_up, bronze = _resolve_ffa_podium(tournament, phase=phase)
    else:
        runner_up, bronze = None, None

    return {
        'runner_up': runner_up,
        'bronze': bronze,
    }


# -- Private helpers for podium resolution -----------------------------------


def _matches_of_phase(
    tournament: Tournament, phase: int | None
) -> list['TournamentMatch']:
    """Return the matches of `phase`, or all of them without one."""
    matches = tournament_match_service.get_matches_for_tournament(tournament.id)
    if phase is None:
        return matches
    return [m for m in matches if m.phase == phase]


def _resolve_bracket_podium(
    tournament: Tournament,
    *,
    phase: int | None = None,
) -> tuple[str | None, str | None]:
    """Derive 2nd and 3rd place from SE or DE bracket matches.

    With a `phase`, only the matches of that phase count.
    """
    matches = _matches_of_phase(tournament, phase)
    if not matches:
        return None, None

    em = tournament_domain_service.elimination_mode_for_phase(
        tournament, phase or 1
    )
    runner_up_name: str | None = None
    bronze_name: str | None = None

    if em == EliminationMode.DOUBLE_ELIMINATION:
        # 2nd place: loser of the LAST Grand Final match (highest round
        # handles bracket-reset scenario).
        gf_matches = [m for m in matches if m.bracket == Bracket.GRAND_FINAL]
        if gf_matches:
            last_gf = max(gf_matches, key=lambda m: (m.round or 0))
            loser_id = _get_match_loser_contestant_id(last_gf)
            if loser_id is not None:
                runner_up_name = _resolve_contestant_name(tournament, loser_id)

        # 3rd place: loser of the LB Final (highest-round LB match).
        lb_matches = [m for m in matches if m.bracket == Bracket.LOSERS]
        if lb_matches:
            lb_final = max(lb_matches, key=lambda m: (m.round or 0))
            loser_id = _get_match_loser_contestant_id(lb_final)
            if loser_id is not None:
                bronze_name = _resolve_contestant_name(tournament, loser_id)

    else:
        # Single Elimination
        # 2nd place: loser of the final (highest-round WB/None match).
        wb_matches = [
            m for m in matches
            if m.bracket in (None, Bracket.WINNERS)
        ]
        if wb_matches:
            final = max(wb_matches, key=lambda m: (m.round or 0))
            loser_id = _get_match_loser_contestant_id(final)
            if loser_id is not None:
                runner_up_name = _resolve_contestant_name(tournament, loser_id)

        # 3rd place: winner of the P3 match.
        p3_matches = [m for m in matches if m.bracket == Bracket.THIRD_PLACE]
        if p3_matches:
            p3_match = p3_matches[0]
            winner_id = _get_match_winner_contestant_id(p3_match)
            if winner_id is not None:
                bronze_name = _resolve_contestant_name(tournament, winner_id)

    return runner_up_name, bronze_name


def _resolve_rr_podium(
    tournament: Tournament,
) -> tuple[str | None, str | None]:
    """Derive 2nd and 3rd place from round-robin standings."""
    if tournament_match_service.is_plain_round_robin(tournament):
        return _resolve_plain_rr_podium(tournament)

    matches = tournament_match_service.get_matches_for_tournament(tournament.id)
    contestants_by_match = tournament_match_service.get_contestants_for_tournament(
        tournament.id
    )

    # Build confirmed match pairs for standings computation.
    confirmed_pairs: list[list] = []
    for match in matches:
        if match.confirmed_by is None:
            continue
        pair = contestants_by_match.get(match.id, [])
        if len(pair) == 2:
            confirmed_pairs.append(pair)

    standings = tournament_domain_service.compute_round_robin_standings(
        confirmed_pairs
    )

    runner_up: str | None = None
    bronze: str | None = None

    if len(standings) > 1:
        runner_up = _resolve_contestant_name(
            tournament, standings[1].contestant_id
        )
    if len(standings) > 2:
        bronze = _resolve_contestant_name(
            tournament, standings[2].contestant_id
        )

    return runner_up, bronze


def _resolve_plain_rr_podium(
    tournament: Tournament,
) -> tuple[str | None, str | None]:
    """Derive 2nd and 3rd place from the ranking; shared ranks are joined."""
    standing = tournament_match_service.plain_round_robin_standing(tournament)

    def _names(rank: int) -> str | None:
        names = [
            _resolve_contestant_name(tournament, entry.contestant_id)
            for entry in standing.ranking.entries
            if entry.rank == rank
        ]
        return ' / '.join(n for n in names if n) or None

    return _names(2), _names(3)


def _resolve_hs_podium(
    tournament: Tournament,
) -> tuple[str | None, str | None]:
    """Derive 2nd and 3rd place from highscore leaderboard."""
    result = tournament_score_service.get_leaderboard(tournament.id)
    if result.is_err():
        return None, None

    leaderboard = result.unwrap()

    runner_up: str | None = None
    bronze: str | None = None

    if len(leaderboard) > 1:
        runner_up = _resolve_score_entry_name(tournament, leaderboard[1])
    if len(leaderboard) > 2:
        bronze = _resolve_score_entry_name(tournament, leaderboard[2])

    return runner_up, bronze


def _resolve_ffa_podium(
    tournament: Tournament,
    *,
    phase: int | None = None,
) -> tuple[str | None, str | None]:
    """Derive 2nd and 3rd place from FFA match placements.

    Looks at all confirmed FFA matches in the tournament (of `phase`,
    if given) and finds the overall 2nd and 3rd place from the final
    round's placements. A double-elimination grand final is the final.
    """
    matches = _matches_of_phase(tournament, phase)
    if not matches:
        return None, None

    confirmed = [m for m in matches if m.confirmed_by is not None]
    if not confirmed:
        return None, None

    grand_finals = [m for m in confirmed if m.bracket == Bracket.GRAND_FINAL]
    if grand_finals:
        final_matches = grand_finals
    else:
        max_round = max((m.round or 0) for m in confirmed)
        final_matches = [m for m in confirmed if (m.round or 0) == max_round]

    runner_up_name: str | None = None
    bronze_name: str | None = None

    for match in final_matches:
        contestants = tournament_match_service.get_contestants_for_match(match.id)
        for c in contestants:
            if c.placement == 2:
                cid = c.team_id or c.participant_id
                if cid is not None:
                    runner_up_name = _resolve_contestant_name(
                        tournament, str(cid)
                    )
            elif c.placement == 3:
                cid = c.team_id or c.participant_id
                if cid is not None:
                    bronze_name = _resolve_contestant_name(
                        tournament, str(cid)
                    )

    return runner_up_name, bronze_name


def _get_match_loser_contestant_id(
    match: 'TournamentMatch',
) -> str | None:
    """Return the loser's contestant ID for a confirmed match.

    Uses score comparison: the contestant with the lower score is the
    loser.  Returns ``None`` if the match has no contestants, is
    unconfirmed, or is a draw.
    """
    contestants = tournament_match_service.get_contestants_for_match(match.id)
    if len(contestants) < 2:
        return None

    # Both must have scores.
    if any(c.score is None for c in contestants):
        return None

    sorted_by_score = sorted(contestants, key=lambda c: c.score, reverse=True)
    # Draw — no loser.
    if sorted_by_score[0].score == sorted_by_score[1].score:
        return None

    loser = sorted_by_score[-1]
    if loser.participant_id is None and loser.team_id is None:
        return None
    return str(loser.participant_id if loser.participant_id is not None else loser.team_id)


def _get_match_winner_contestant_id(
    match: 'TournamentMatch',
) -> str | None:
    """Return the winner's contestant ID for a confirmed match.

    Returns ``None`` if the match has no contestants, is unconfirmed,
    or is a draw.
    """
    contestants = tournament_match_service.get_contestants_for_match(match.id)
    if len(contestants) < 2:
        return None

    if any(c.score is None for c in contestants):
        return None

    sorted_by_score = sorted(contestants, key=lambda c: c.score, reverse=True)
    if sorted_by_score[0].score == sorted_by_score[1].score:
        return None

    winner = sorted_by_score[0]
    if winner.participant_id is None and winner.team_id is None:
        return None
    return str(winner.participant_id if winner.participant_id is not None else winner.team_id)


def _resolve_contestant_name(
    tournament: Tournament,
    contestant_id: str,
) -> str | None:
    """Resolve a contestant UUID string to a display name.

    Works for both team-based and participant-based tournaments.
    """
    from uuid import UUID

    if tournament.contestant_type == ContestantType.TEAM:
        from .models.tournament_team import TournamentTeamID

        team = tournament_team_service.find_team(
            TournamentTeamID(UUID(contestant_id))
        )
        return team.name if team is not None else None
    else:
        from byceps.services.user import user_service
        from .models.tournament_participant import TournamentParticipantID

        participant = tournament_participant_service.find_participant(
            TournamentParticipantID(UUID(contestant_id))
        )
        if participant is None:
            return None
        user = user_service.get_user(participant.user_id)
        return user.screen_name


def _resolve_score_entry_name(
    tournament: Tournament,
    entry: 'ScoreSubmission',
) -> str | None:
    """Resolve a highscore leaderboard entry to a display name."""
    contestant_id = entry.team_id if entry.team_id is not None else entry.participant_id
    if contestant_id is None:
        return None
    return _resolve_contestant_name(tournament, str(contestant_id))
