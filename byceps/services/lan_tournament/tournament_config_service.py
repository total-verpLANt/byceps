"""
byceps.services.lan_tournament.tournament_config_service
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from dataclasses import dataclass
from hashlib import sha256
import math
from uuid import UUID

from byceps.services.lan_tournament import (
    tournament_config_document,
    tournament_config_domain_service,
    tournament_image_service,
    tournament_service,
)
from byceps.services.lan_tournament.events import TournamentCreatedEvent
from byceps.services.lan_tournament.models.tournament import Tournament
from byceps.services.lan_tournament.models.tournament_image import (
    TournamentImageID,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.lan_tournament.models.validation_message import (
    ValidationMessage,
)
from byceps.services.lan_tournament.tournament_config_document import (
    DocumentProblem,
)
from byceps.services.lan_tournament.tournament_config_domain_service import (
    TournamentConfig,
)
from byceps.services.lan_tournament.tournament_log_service import (
    create_log_entry,
)
from byceps.services.party.models import PartyID
from byceps.services.user.models import UserID
from byceps.util.result import Err, Ok, Result


CONFIG_EXPORTED_EVENT = 'tournament-config-exported'
CONFIG_IMPORTED_EVENT = 'tournament-config-imported'

# The import POST carries a maximum image, a maximum document as base64
# and the small form fields.
_IMPORT_OVERHEAD_BYTES = 64 * 1024
IMPORT_MAX_REQUEST_BYTES = (
    tournament_image_service.MAX_UPLOAD_BYTES
    + 4 * math.ceil(tournament_config_document.MAX_DOCUMENT_BYTES / 3)
    + _IMPORT_OVERHEAD_BYTES
)


@dataclass(frozen=True, kw_only=True)
class ImportCheck:
    config: TournamentConfig
    document_sha256: str


def export_tournament_config(
    tournament: Tournament, initiator_id: UserID
) -> bytes:
    """Return the configuration document of `tournament` and log the export."""
    document = tournament_config_document.serialize(
        tournament_config_domain_service.config_input_of(tournament)
    )

    create_log_entry(
        CONFIG_EXPORTED_EVENT,
        tournament.id,
        initiator_id,
        data=_log_data(sha256(document).hexdigest()),
        commit=True,
    )

    return document


def check_import(raw: bytes) -> Result[ImportCheck, list[DocumentProblem]]:
    """Parse and check a document; write nothing."""
    parsed = tournament_config_document.parse_document(raw)
    if parsed.is_err():
        return Err(parsed.unwrap_err())

    normalized = tournament_config_domain_service.normalize_config(
        parsed.unwrap()
    )
    if normalized.is_err():
        return Err(
            [
                DocumentProblem(location=field, message=message)
                for field, message in normalized.unwrap_err().items()
            ]
        )

    return Ok(
        ImportCheck(
            config=normalized.unwrap(),
            document_sha256=sha256(raw).hexdigest(),
        )
    )


def import_tournament_config(
    party_id: PartyID,
    check: ImportCheck,
    initiator_id: UserID,
    *,
    image_id: TournamentImageID | None = None,
    image_alt_text: str | None = None,
    creation_token: UUID | None = None,
) -> Result[tuple[Tournament, TournamentCreatedEvent], str | ValidationMessage]:
    """Create a draft tournament from a checked document."""
    config = check.config
    settings = config.settings

    return tournament_service.create_tournament(
        party_id,
        config.name,
        category=config.category,
        game=config.game,
        description=config.description,
        ruleset=config.ruleset,
        start_time=config.start_time,
        contestant_type=settings.contestant_type,
        game_format=settings.game_format,
        elimination_mode=settings.elimination_mode,
        score_ordering=settings.score_ordering,
        min_players=settings.min_players,
        max_players=settings.max_players,
        min_teams=settings.min_teams,
        max_teams=settings.max_teams,
        min_players_in_team=settings.min_players_in_team,
        max_players_in_team=settings.max_players_in_team,
        point_table=settings.point_table,
        advancement_count=settings.advancement_count,
        group_size_min=settings.group_size_min,
        group_size_max=settings.group_size_max,
        points_carry_to_losers=config.points_carry_to_losers,
        playoff_game_format=settings.playoff_game_format,
        playoff_elimination_mode=settings.playoff_elimination_mode,
        playoff_group_count=settings.playoff_group_count,
        playoff_qualifiers_per_group=settings.playoff_qualifiers_per_group,
        playoff_qualifier_count=settings.playoff_qualifier_count,
        playoff_release_mode=settings.playoff_release_mode,
        tournament_status=TournamentStatus.DRAFT,
        initiator_id=initiator_id,
        image_id=image_id,
        image_alt_text=image_alt_text,
        creation_token=creation_token,
        log_event_type=CONFIG_IMPORTED_EVENT,
        log_data=_log_data(check.document_sha256),
    )


def _log_data(document_sha256: str) -> dict[str, str | int]:
    return {
        'format_version': tournament_config_document.VERSION,
        'document_sha256': document_sha256,
    }
