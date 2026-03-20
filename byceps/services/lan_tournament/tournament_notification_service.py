"""
byceps.services.lan_tournament.tournament_notification_service
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Email notifications for tournament matches.

:Copyright: 2014-2026 Jochen Kupperschmidt
:License: Revised BSD (see `LICENSE` file for details)
"""

import structlog

from byceps.services.brand import brand_service
from byceps.services.brand.models import Brand
from byceps.services.email import (
    email_config_service,
    email_footer_service,
    email_service,
)
from byceps.services.email.models import Message, NameAndAddress
from byceps.services.party import party_service
from byceps.services.snippet import snippet_service
from byceps.services.snippet.models import SnippetScope
from byceps.services.user import user_service
from byceps.services.user.models import User, UserID
from byceps.util.l10n import get_default_locale

from . import tournament_participant_service, tournament_repository
from .models.tournament import TournamentID
from .models.tournament_match import TournamentMatchID
from .models.tournament_match_to_contestant import TournamentMatchToContestant


log = structlog.get_logger()


SNIPPET_NAME_BODY = 'email_match_ready_body'
SNIPPET_NAME_SUBJECT = 'email_match_ready_subject'


def send_match_ready_emails(
    tournament_id: TournamentID,
    match_id: TournamentMatchID,
) -> None:
    """Send email notifications to all participants in a match.

    Resolves contestants to users, assembles personalised messages,
    and enqueues them for delivery.  Fails gracefully: snippet or
    user-data errors are logged but never propagate as exceptions.
    """
    # 1-4: Resolve tournament → party → brand → email config.
    tournament = tournament_repository.get_tournament(tournament_id)
    party = party_service.get_party(tournament.party_id)
    brand = brand_service.get_brand(party.brand_id)
    email_config = email_config_service.get_config(party.brand_id)

    # 5-6: Get match and its contestants.
    match = tournament_repository.get_match(match_id)
    contestants = tournament_repository.get_contestants_for_match(match_id)

    if len(contestants) < 2:
        log.warning(
            'Match has fewer than 2 contestants, skipping notifications',
            match_id=str(match_id),
        )
        return

    # For each contestant, build the set of user_ids to notify and
    # track which contestant they belong to (needed for opponent lookup).
    contestant_user_map: dict[
        int, tuple[TournamentMatchToContestant, list[UserID]]
    ] = {}
    for idx, contestant in enumerate(contestants):
        user_ids = _resolve_user_ids_for_contestant(contestant)
        contestant_user_map[idx] = (contestant, user_ids)

    # Collect all user_ids for seat lookup.
    all_user_ids: set[UserID] = set()
    for _, user_ids in contestant_user_map.values():
        all_user_ids.update(user_ids)

    # 10: Batch-fetch seat labels.
    seats = tournament_participant_service.get_seats_for_users(
        all_user_ids, tournament.party_id
    )

    # For each contestant side, send emails to its users.
    for idx, (contestant, user_ids) in contestant_user_map.items():
        # Determine opponent contestant (the other side).
        opponent_idx = 1 - idx if len(contestants) == 2 else None
        if opponent_idx is None:
            continue
        opponent_contestant = contestant_user_map[opponent_idx][0]

        # 9: Build opponent display name.
        opponent_name = _build_opponent_display_name(opponent_contestant)

        # Opponent seat: for teams use captain's seat, for solo use
        # the opponent user's seat.
        opponent_seat = _get_opponent_seat(opponent_contestant, seats)

        for user_id in user_ids:
            _send_email_to_user(
                user_id=user_id,
                sender=email_config.sender,
                brand=brand,
                tournament_name=tournament.name,
                match_round=match.round,
                opponent_name=opponent_name,
                opponent_seat=opponent_seat,
                your_seat=seats.get(user_id, '?'),
            )


def _resolve_user_ids_for_contestant(
    contestant: TournamentMatchToContestant,
) -> list[UserID]:
    """Return all user IDs that should be notified for a contestant."""
    if contestant.team_id is not None:
        participants = tournament_repository.get_participants_for_team(
            contestant.team_id
        )
        return [p.user_id for p in participants]

    if contestant.participant_id is not None:
        participant = tournament_repository.get_participant(
            contestant.participant_id
        )
        return [participant.user_id]

    return []


def _build_opponent_display_name(
    contestant: TournamentMatchToContestant,
) -> str:
    """Return a human-readable name for the opponent."""
    if contestant.team_id is not None:
        team = tournament_repository.get_team(contestant.team_id)
        captain = user_service.get_user(team.captain_user_id)
        captain_name = captain.screen_name or 'Unknown'
        return f'{team.name} (captain: {captain_name})'

    if contestant.participant_id is not None:
        participant = tournament_repository.get_participant(
            contestant.participant_id
        )
        user = user_service.get_user(participant.user_id)
        return user.screen_name or 'Unknown'

    return 'TBD'


def _get_opponent_seat(
    contestant: TournamentMatchToContestant,
    seats: dict[UserID, str],
) -> str:
    """Return a seat label for the opponent side."""
    if contestant.team_id is not None:
        team = tournament_repository.get_team(contestant.team_id)
        return seats.get(team.captain_user_id, '?')

    if contestant.participant_id is not None:
        participant = tournament_repository.get_participant(
            contestant.participant_id
        )
        return seats.get(participant.user_id, '?')

    return '?'


def _send_email_to_user(
    *,
    user_id: UserID,
    sender: NameAndAddress,
    brand: Brand,
    tournament_name: str,
    match_round: int | None,
    opponent_name: str,
    opponent_seat: str,
    your_seat: str,
) -> None:
    """Assemble and enqueue one match-ready email for a single user."""
    email_address = user_service.find_email_address(user_id)
    if email_address is None:
        log.warning(
            'User has no email address, skipping match-ready notification',
            user_id=str(user_id),
        )
        return

    locale = user_service.find_locale(user_id) or get_default_locale()
    language_code = locale.language

    # Fetch snippet body template.
    scope = SnippetScope.for_brand(brand.id)
    body_result = snippet_service.get_snippet_body(
        scope, SNIPPET_NAME_BODY, language_code
    )
    if body_result.is_err():
        log.error(
            'Match-ready email body snippet not found, skipping',
            brand_id=str(brand.id),
            language_code=language_code,
            error=str(body_result.unwrap_err()),
        )
        return

    body_template = body_result.unwrap()

    # Fetch snippet subject template.
    subject_result = snippet_service.get_snippet_body(
        scope, SNIPPET_NAME_SUBJECT, language_code
    )
    if subject_result.is_err():
        log.error(
            'Match-ready email subject snippet not found, skipping',
            brand_id=str(brand.id),
            language_code=language_code,
            error=str(subject_result.unwrap_err()),
        )
        return

    subject_template = subject_result.unwrap()

    # Fetch email footer.
    footer_result = email_footer_service.get_footer(brand, language_code)
    if footer_result.is_err():
        log.error(
            'Email footer not found, skipping match-ready notification',
            brand_id=str(brand.id),
            language_code=language_code,
        )
        return

    footer = footer_result.unwrap()

    round_display = str(match_round) if match_round is not None else '?'

    # Escape curly braces in user-controlled values so they cannot
    # interfere with str.format() placeholders (format string injection).
    def _esc(value: str) -> str:
        return value.replace('{', '{{').replace('}', '}}')

    safe_tournament_name = _esc(tournament_name)
    safe_opponent_name = _esc(opponent_name)
    safe_opponent_seat = _esc(opponent_seat)
    safe_your_seat = _esc(your_seat)

    try:
        body = body_template.format(
            tournament_name=safe_tournament_name,
            match_round=round_display,
            opponent_name=safe_opponent_name,
            opponent_seat=safe_opponent_seat,
            your_seat=safe_your_seat,
            footer=footer,
        )

        subject = subject_template.format(
            tournament_name=safe_tournament_name,
            match_round=round_display,
            opponent_name=safe_opponent_name,
            opponent_seat=safe_opponent_seat,
            your_seat=safe_your_seat,
            footer=footer,
        )
    except (KeyError, ValueError) as exc:
        log.error(
            'Failed to format match-ready email template, skipping',
            user_id=str(user_id),
            error=str(exc),
        )
        return

    message = Message(
        sender=sender,
        recipients=[email_address],
        subject=subject,
        body=body,
    )
    email_service.enqueue_message(message)


# -- snippet setup ---------------------------------------------------


def create_match_ready_email_snippets(
    brand: Brand,
    creator: User,
) -> bool:
    """Idempotently create default email snippets for match-ready notifications.

    Returns True if snippets were created, False if they already exist.
    """
    scope = SnippetScope.for_brand(brand.id)

    language_codes_and_bodies = [
        (
            'en',
            (
                'Your match in "{tournament_name}" (round {match_round}) is ready!\n'
                '\n'
                'Opponent: {opponent_name}\n'
                'Opponent seat: {opponent_seat}\n'
                'Your seat: {your_seat}\n'
                '\n'
                '{footer}'
            ),
        ),
        (
            'de',
            (
                'Dein Match im Turnier "{tournament_name}" (Runde {match_round}) ist bereit!\n'
                '\n'
                'Gegner: {opponent_name}\n'
                'Gegner-Sitzplatz: {opponent_seat}\n'
                'Dein Sitzplatz: {your_seat}\n'
                '\n'
                '{footer}'
            ),
        ),
    ]

    language_codes_and_subjects = [
        (
            'en',
            '[{tournament_name}] Your match (round {match_round}) is ready!',
        ),
        (
            'de',
            '[{tournament_name}] Dein Match (Runde {match_round}) ist bereit!',
        ),
    ]

    created_any = False

    for language_code, body in language_codes_and_bodies:
        existing = snippet_service.find_current_version_of_snippet_with_name(
            scope, SNIPPET_NAME_BODY, language_code
        )
        if existing is None:
            snippet_service.create_snippet(
                scope, SNIPPET_NAME_BODY, language_code, creator, body
            )
            created_any = True

    for language_code, subject in language_codes_and_subjects:
        existing = snippet_service.find_current_version_of_snippet_with_name(
            scope, SNIPPET_NAME_SUBJECT, language_code
        )
        if existing is None:
            snippet_service.create_snippet(
                scope, SNIPPET_NAME_SUBJECT, language_code, creator, subject
            )
            created_any = True

    return created_any
