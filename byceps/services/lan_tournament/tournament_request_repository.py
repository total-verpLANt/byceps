"""
byceps.services.lan_tournament.tournament_request_repository
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from collections.abc import Iterable
from datetime import datetime, UTC

from sqlalchemy import func, select

from byceps.database import db
from byceps.services.party.models import PartyID
from byceps.services.user.models import UserID

from .dbmodels.tournament_request import DbTournamentRequest
from .models.elimination_mode import EliminationMode
from .models.game_format import GameFormat
from .models.tournament import TournamentID
from .models.tournament_request import (
    TournamentRequest,
    TournamentRequestID,
    TournamentRequestStatus,
)
from .tournament_repository import _safe_enum_lookup


def create_request(request: TournamentRequest) -> None:
    """Persist a tournament request (flush only -- caller commits)."""
    db_request = DbTournamentRequest(
        request.id,
        request.party_id,
        request.number,
        request.proposer_id,
        request.created_at,
        request.status.value,
        request.name,
        request.game,
        request.game_format.value,
        request.elimination_mode.value,
        request.team_size,
        request.participant_limit,
        request.preferred_start_time,
        request.preferred_end_time,
        request.description,
        updated_at=request.updated_at,
        special_rules=request.special_rules,
        notes=request.notes,
        desired_template=request.desired_template,
        decided_at=request.decided_at,
        decided_by_id=request.decided_by_id,
        rejection_reason=request.rejection_reason,
        created_tournament_id=request.created_tournament_id,
    )

    db.session.add(db_request)
    db.session.flush()


def find_request(
    request_id: TournamentRequestID,
) -> TournamentRequest | None:
    """Return the tournament request, or `None` if not found."""
    db_request = db.session.get(DbTournamentRequest, request_id)
    if db_request is None:
        return None
    return _db_request_to_request(db_request)


def get_request_for_update(
    request_id: TournamentRequestID,
) -> TournamentRequest:
    """Return the tournament request with row lock for update.

    Raise an exception if not found.
    """
    db_request = db.session.execute(
        select(DbTournamentRequest).filter_by(id=request_id).with_for_update()
    ).scalar_one_or_none()
    if db_request is None:
        raise ValueError(f'Unknown tournament request ID "{request_id}"')
    return _db_request_to_request(db_request)


def get_requests_for_party(
    party_id: PartyID,
    *,
    status: TournamentRequestStatus | None = None,
) -> list[TournamentRequest]:
    """Return all requests for that party, optionally filtered by status."""
    stmt = (
        select(DbTournamentRequest)
        .filter_by(party_id=party_id)
        .order_by(DbTournamentRequest.number)
    )
    if status is not None:
        stmt = stmt.where(DbTournamentRequest.status == status.value)
    db_requests = db.session.execute(stmt).scalars().all()
    return [_db_request_to_request(r) for r in db_requests]


def get_requests_for_proposer(
    party_id: PartyID,
    proposer_id: UserID,
) -> list[TournamentRequest]:
    """Return all requests for that proposer within that party."""
    db_requests = (
        db.session.execute(
            select(DbTournamentRequest)
            .filter_by(party_id=party_id, proposer_id=proposer_id)
            .order_by(DbTournamentRequest.number)
        )
        .scalars()
        .all()
    )
    return [_db_request_to_request(r) for r in db_requests]


def get_next_number_for_party(party_id: PartyID) -> int:
    """Return the next sequential request number for that party."""
    return db.session.execute(
        select(
            func.coalesce(func.max(DbTournamentRequest.number), 0) + 1
        ).filter_by(party_id=party_id)
    ).scalar_one()


def count_open_requests_for_proposer(
    party_id: PartyID,
    proposer_id: UserID,
) -> int:
    """Return the number of that proposer's requests still `submitted`."""
    return db.session.execute(
        select(func.count(DbTournamentRequest.id)).where(
            DbTournamentRequest.party_id == party_id,
            DbTournamentRequest.proposer_id == proposer_id,
            DbTournamentRequest.status
            == TournamentRequestStatus.submitted.value,
        )
    ).scalar_one()


def count_requests_for_party_with_statuses(
    party_id: PartyID,
    statuses: Iterable[TournamentRequestStatus],
) -> int:
    """Return the number of that party's requests in any of the statuses."""
    status_values = [status.value for status in statuses]
    return db.session.execute(
        select(func.count(DbTournamentRequest.id)).where(
            DbTournamentRequest.party_id == party_id,
            DbTournamentRequest.status.in_(status_values),
        )
    ).scalar_one()


def update_request_flush(request: TournamentRequest) -> None:
    """Update a tournament request in place (flush only -- caller commits)."""
    db_request = db.session.get(DbTournamentRequest, request.id)
    if db_request is None:
        raise ValueError(f'Unknown tournament request ID "{request.id}"')

    db_request.updated_at = request.updated_at
    db_request.status = request.status.value
    db_request.name = request.name
    db_request.game = request.game
    db_request.game_format = request.game_format.value
    db_request.elimination_mode = request.elimination_mode.value
    db_request.team_size = request.team_size
    db_request.participant_limit = request.participant_limit
    db_request.preferred_start_time = request.preferred_start_time
    db_request.preferred_end_time = request.preferred_end_time
    db_request.description = request.description
    db_request.special_rules = request.special_rules
    db_request.notes = request.notes
    db_request.desired_template = request.desired_template
    db_request.decided_at = request.decided_at
    db_request.decided_by_id = request.decided_by_id
    db_request.rejection_reason = request.rejection_reason
    db_request.created_tournament_id = request.created_tournament_id

    db.session.flush()


def unlink_created_tournament_flush(
    tournament_id: TournamentID,
) -> list[TournamentRequest]:
    """Clear the link to a tournament that is being deleted.

    Sets `created_tournament_id` to `NULL` on every request that
    points at that tournament (flush only -- caller commits). At most
    one request is expected to link to a given tournament, but every
    match found is cleared. `status` and the `decided_*` fields are
    left untouched -- the request stays `tournament_created` as
    history.
    """
    db_requests = (
        db.session.execute(
            select(DbTournamentRequest).filter_by(
                created_tournament_id=tournament_id
            )
        )
        .scalars()
        .all()
    )

    now = datetime.now(UTC)
    for db_request in db_requests:
        db_request.created_tournament_id = None
        db_request.updated_at = now

    db.session.flush()

    return [_db_request_to_request(r) for r in db_requests]


def _db_request_to_request(
    db_request: DbTournamentRequest,
) -> TournamentRequest:
    status = _safe_enum_lookup(TournamentRequestStatus, db_request.status)
    if status is None:
        raise ValueError(
            f'Invalid tournament request status {db_request.status!r} '
            f'for request "{db_request.id}"'
        )

    game_format = _safe_enum_lookup(GameFormat, db_request.game_format)
    if game_format is None:
        raise ValueError(
            f'Invalid game format {db_request.game_format!r} '
            f'for request "{db_request.id}"'
        )

    elimination_mode = _safe_enum_lookup(
        EliminationMode, db_request.elimination_mode
    )
    if elimination_mode is None:
        raise ValueError(
            f'Invalid elimination mode {db_request.elimination_mode!r} '
            f'for request "{db_request.id}"'
        )

    return TournamentRequest(
        id=db_request.id,
        party_id=db_request.party_id,
        number=db_request.number,
        proposer_id=db_request.proposer_id,
        created_at=db_request.created_at,
        updated_at=db_request.updated_at,
        status=status,
        name=db_request.name,
        game=db_request.game,
        game_format=game_format,
        elimination_mode=elimination_mode,
        team_size=db_request.team_size,
        participant_limit=db_request.participant_limit,
        preferred_start_time=db_request.preferred_start_time,
        preferred_end_time=db_request.preferred_end_time,
        description=db_request.description,
        special_rules=db_request.special_rules,
        notes=db_request.notes,
        desired_template=db_request.desired_template,
        decided_at=db_request.decided_at,
        decided_by_id=db_request.decided_by_id,
        rejection_reason=db_request.rejection_reason,
        created_tournament_id=db_request.created_tournament_id,
    )
