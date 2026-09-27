"""
byceps.services.lan_tournament.tournament_notification_service
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Email notifications for tournament matches.
"""

from sqlalchemy.exc import IntegrityError
import structlog

from byceps.database import db
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
from .models.tournament_request import TournamentRequest


log = structlog.get_logger()


SNIPPET_NAME_BODY = 'email_match_ready_body'
SNIPPET_NAME_SUBJECT = 'email_match_ready_subject'
SNIPPET_NAME_REQUEST_ACCEPTED_BODY = 'email_tournament_request_accepted_body'
SNIPPET_NAME_REQUEST_ACCEPTED_SUBJECT = (
    'email_tournament_request_accepted_subject'
)
SNIPPET_NAME_REQUEST_REJECTED_BODY = 'email_tournament_request_rejected_body'
SNIPPET_NAME_REQUEST_REJECTED_SUBJECT = (
    'email_tournament_request_rejected_subject'
)


def send_match_ready_emails(
    tournament_id: TournamentID,
    match_id: TournamentMatchID,
) -> None:
    """Send email notifications to all participants in a match.

    Resolves contestants to users, assembles personalised messages,
    and enqueues them for delivery. Each recipient is isolated: an
    unexpected error while sending to one recipient is logged and the
    remaining recipients of the match still get their email.
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
            try:
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
            except Exception:
                log.exception(
                    'Unexpected error sending match-ready email, '
                    'skipping recipient',
                    tournament_id=str(tournament_id),
                    match_id=str(match_id),
                    user_id=str(user_id),
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


def _escape_format_braces(value: str) -> str:
    """Escape curly braces so a user-controlled value cannot interfere
    with `str.format()` placeholders (format string injection).
    """
    return value.replace('{', '{{').replace('}', '}}')


def _fetch_email_content_for_language(
    *,
    brand: Brand,
    scope: SnippetScope,
    body_snippet_name: str,
    subject_snippet_name: str,
    language_code: str,
    notification_label: str,
    log_fields: dict[str, str],
    quiet: bool,
) -> tuple[str, str, str] | None:
    """Fetch the body/subject snippets and footer for one language.

    Returns `None` if any of the three is missing for that language, so
    a caller trying several languages never mixes templates from
    different ones within a single email. Failures are logged unless
    `quiet` -- used while a further fallback language is still to be
    tried, so a routine fallback does not read as an error.
    """
    body_result = snippet_service.get_snippet_body(
        scope, body_snippet_name, language_code
    )
    if body_result.is_err():
        if not quiet:
            log.error(
                '%s email body snippet not found, skipping',
                notification_label.capitalize(),
                brand_id=str(brand.id),
                language_code=language_code,
                error=str(body_result.unwrap_err()),
                **log_fields,
            )
        return None

    subject_result = snippet_service.get_snippet_body(
        scope, subject_snippet_name, language_code
    )
    if subject_result.is_err():
        if not quiet:
            log.error(
                '%s email subject snippet not found, skipping',
                notification_label.capitalize(),
                brand_id=str(brand.id),
                language_code=language_code,
                error=str(subject_result.unwrap_err()),
                **log_fields,
            )
        return None

    footer_result = email_footer_service.get_footer(brand, language_code)
    if footer_result.is_err():
        if not quiet:
            log.error(
                'Email footer not found, skipping %s notification',
                notification_label,
                brand_id=str(brand.id),
                language_code=language_code,
                **log_fields,
            )
        return None

    return (
        body_result.unwrap(),
        subject_result.unwrap(),
        footer_result.unwrap(),
    )


def _send_formatted_email(
    *,
    user_id: UserID,
    sender: NameAndAddress,
    brand: Brand,
    body_snippet_name: str,
    subject_snippet_name: str,
    format_kwargs: dict[str, str],
    notification_label: str,
    extra_log_fields: dict[str, str] | None = None,
) -> None:
    """Fetch the body/subject snippets and footer for one user, format
    them, and enqueue the resulting email.

    Shared tail of the match-ready and tournament-request-decision
    email paths. Tries the recipient's own language first, falling
    back once to the app's default locale language if body, subject or
    footer is missing in it; if the fallback is also missing (or is
    the same language), the failure is logged and nothing is sent.
    Catches the exceptions a malformed admin-edited snippet's
    `.format()` call can raise. It does not guard against other,
    unexpected errors (e.g. from user or brand lookups) -- callers
    isolate those per recipient.
    """
    log_fields = extra_log_fields or {}

    email_address = user_service.find_email_address(user_id)
    if email_address is None:
        log.warning(
            'User has no email address, skipping %s notification',
            notification_label,
            user_id=str(user_id),
            **log_fields,
        )
        return

    locale = user_service.find_locale(user_id) or get_default_locale()
    language_code = locale.language
    default_language_code = get_default_locale().language

    language_codes_to_try = [language_code]
    if default_language_code != language_code:
        language_codes_to_try.append(default_language_code)

    scope = SnippetScope.for_brand(brand.id)

    content = None
    for index, candidate_language_code in enumerate(language_codes_to_try):
        is_last_attempt = index == len(language_codes_to_try) - 1
        content = _fetch_email_content_for_language(
            brand=brand,
            scope=scope,
            body_snippet_name=body_snippet_name,
            subject_snippet_name=subject_snippet_name,
            language_code=candidate_language_code,
            notification_label=notification_label,
            log_fields=log_fields,
            quiet=not is_last_attempt,
        )
        if content is not None:
            break

    if content is None:
        return

    body_template, subject_template, footer = content

    try:
        body = body_template.format(footer=footer, **format_kwargs)
        subject = subject_template.format(footer=footer, **format_kwargs)
    except (
        KeyError,
        ValueError,
        IndexError,
        AttributeError,
        TypeError,
    ) as exc:
        log.error(
            'Failed to format %s email template, skipping',
            notification_label,
            user_id=str(user_id),
            error=str(exc),
            **log_fields,
        )
        return

    message = Message(
        sender=sender,
        recipients=[email_address],
        subject=subject,
        body=body,
    )
    email_service.enqueue_message(message)


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
    round_display = str(match_round) if match_round is not None else '?'

    format_kwargs = {
        'tournament_name': _escape_format_braces(tournament_name),
        'match_round': round_display,
        'opponent_name': _escape_format_braces(opponent_name),
        'opponent_seat': _escape_format_braces(opponent_seat),
        'your_seat': _escape_format_braces(your_seat),
    }

    _send_formatted_email(
        user_id=user_id,
        sender=sender,
        brand=brand,
        body_snippet_name=SNIPPET_NAME_BODY,
        subject_snippet_name=SNIPPET_NAME_SUBJECT,
        format_kwargs=format_kwargs,
        notification_label='match-ready',
    )


def send_request_accepted_email(
    brand: Brand,
    request: TournamentRequest,
) -> None:
    """Notify the proposer by email that their tournament request was
    accepted.
    """
    email_config = email_config_service.get_config(brand.id)

    format_kwargs = {
        'request_name': _escape_format_braces(request.name),
    }

    _send_formatted_email(
        user_id=request.proposer_id,
        sender=email_config.sender,
        brand=brand,
        body_snippet_name=SNIPPET_NAME_REQUEST_ACCEPTED_BODY,
        subject_snippet_name=SNIPPET_NAME_REQUEST_ACCEPTED_SUBJECT,
        format_kwargs=format_kwargs,
        notification_label='request-accepted',
        extra_log_fields={'request_id': str(request.id)},
    )


def send_request_rejected_email(
    brand: Brand,
    request: TournamentRequest,
) -> None:
    """Notify the proposer by email that their tournament request was
    rejected, including the reason given for the rejection.
    """
    email_config = email_config_service.get_config(brand.id)

    format_kwargs = {
        'request_name': _escape_format_braces(request.name),
        'reason': _escape_format_braces(request.rejection_reason or ''),
    }

    _send_formatted_email(
        user_id=request.proposer_id,
        sender=email_config.sender,
        brand=brand,
        body_snippet_name=SNIPPET_NAME_REQUEST_REJECTED_BODY,
        subject_snippet_name=SNIPPET_NAME_REQUEST_REJECTED_SUBJECT,
        format_kwargs=format_kwargs,
        notification_label='request-rejected',
        extra_log_fields={'request_id': str(request.id)},
    )


# -- snippet setup ---------------------------------------------------


# Single source of truth for every (snippet_name, language_code) pair a
# brand needs for tournament notifications. `email_templates_exist`
# derives from this tuple directly; the two `create_*_email_snippets`
# functions list the same pairs by hand as `contents_by_pair` keys, so
# a unit test pins that the two stay in sync with this tuple.
# fmt: off
_REQUIRED_SNIPPET_PAIRS: tuple[tuple[str, str], ...] = (
    (SNIPPET_NAME_BODY,                     'en'),
    (SNIPPET_NAME_BODY,                     'de'),
    (SNIPPET_NAME_SUBJECT,                  'en'),
    (SNIPPET_NAME_SUBJECT,                  'de'),
    (SNIPPET_NAME_REQUEST_ACCEPTED_BODY,    'en'),
    (SNIPPET_NAME_REQUEST_ACCEPTED_BODY,    'de'),
    (SNIPPET_NAME_REQUEST_ACCEPTED_SUBJECT, 'en'),
    (SNIPPET_NAME_REQUEST_ACCEPTED_SUBJECT, 'de'),
    (SNIPPET_NAME_REQUEST_REJECTED_BODY,    'en'),
    (SNIPPET_NAME_REQUEST_REJECTED_BODY,    'de'),
    (SNIPPET_NAME_REQUEST_REJECTED_SUBJECT, 'en'),
    (SNIPPET_NAME_REQUEST_REJECTED_SUBJECT, 'de'),
)
# fmt: on


def _find_missing_snippets(
    brand: Brand,
    pairs: tuple[tuple[str, str], ...] = _REQUIRED_SNIPPET_PAIRS,
) -> list[tuple[str, str]]:
    """Return the `(snippet_name, language_code)` pairs, out of `pairs`,
    that don't yet exist for the brand.

    Fetches every snippet the brand's scope currently has in a single
    call instead of one lookup per pair (12 pairs otherwise means 12
    queries on every admin overview render).
    """
    scope = SnippetScope.for_brand(brand.id)
    existing_snippets = (
        snippet_service.get_snippets_for_scope_with_current_versions(scope)
    )
    existing_pairs = {
        (snippet.name, snippet.language_code)
        for snippet in existing_snippets
    }
    return [pair for pair in pairs if pair not in existing_pairs]


def _create_missing_snippets(
    brand: Brand,
    creator: User,
    contents_by_pair: dict[tuple[str, str], str],
) -> bool:
    """Create whichever of the given snippets don't exist yet.

    Never overwrites or re-versions an existing snippet, so any text an
    admin has already edited is preserved. If two setup requests race
    on the same pair, the loser's `IntegrityError` (the snippets table
    has a unique constraint on scope/name/language) is treated as the
    pair already having been created only once a recheck confirms it
    now exists. Any other cause of the same exception (e.g. a missing
    language row violating a FK) is not a race and re-raises, so a real
    config error surfaces loudly instead of being logged and swallowed.
    Returns whether this call itself created any snippet.
    """
    scope = SnippetScope.for_brand(brand.id)
    missing = _find_missing_snippets(brand, tuple(contents_by_pair))

    created_any = False
    for name, language_code in missing:
        try:
            snippet_service.create_snippet(
                scope,
                name,
                language_code,
                creator,
                contents_by_pair[(name, language_code)],
            )
            created_any = True
        except IntegrityError:
            db.session.rollback()
            already_created = (
                snippet_service.find_current_version_of_snippet_with_name(
                    scope, name, language_code
                )
                is not None
            )
            if already_created:
                log.info(
                    'Snippet was already created by a concurrent setup, '
                    'skipping',
                    brand_id=str(brand.id),
                    snippet_name=name,
                    language_code=language_code,
                )
            else:
                log.error(
                    'Snippet creation failed and the pair is still '
                    'missing',
                    brand_id=str(brand.id),
                    snippet_name=name,
                    language_code=language_code,
                )
                raise

    return created_any


def create_match_ready_email_snippets(
    brand: Brand,
    creator: User,
) -> bool:
    """Idempotently create default email snippets for match-ready notifications.

    Returns True if snippets were created, False if they already exist.
    """
    contents_by_pair = {
        (SNIPPET_NAME_BODY, 'en'): (
            '\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n'
            '\U0001f3c1 MATCH READY\n'
            '\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n'
            '\n'
            'Your match in {tournament_name} is ready.\n'
            '\n'
            'Round: {match_round}\n'
            '\n'
            '\U0001f464 Opponent: {opponent_name}\n'
            '\U0001fa91 Your seat: {your_seat}\n'
            '\U0001f4cd Opponent\'s seat: {opponent_seat}\n'
            '\n'
            'Coordinate with your opponent and contact the tournament staff if you run into any problems.\n'
            '\n'
            '{footer}'
        ),
        (SNIPPET_NAME_BODY, 'de'): (
            '\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n'
            '\U0001f3c1 MATCH BEREIT\n'
            '\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n'
            '\n'
            'Dein Match in {tournament_name} ist jetzt bereit.\n'
            '\n'
            'Runde: {match_round}\n'
            '\n'
            '\U0001f464 Gegner: {opponent_name}\n'
            '\U0001fa91 Dein Platz: {your_seat}\n'
            '\U0001f4cd Platz deines Gegners: {opponent_seat}\n'
            '\n'
            'Stimme dich mit deinem Gegner ab und wende dich bei Problemen an die Turnier-Orga.\n'
            '\n'
            '{footer}'
        ),
        (SNIPPET_NAME_SUBJECT, 'en'): (
            '[{tournament_name}] Your match (round {match_round}) is ready! \U0001f3c1'
        ),
        (SNIPPET_NAME_SUBJECT, 'de'): (
            '[{tournament_name}] Dein Match (Runde {match_round}) ist bereit! \U0001f3c1'
        ),
    }

    return _create_missing_snippets(brand, creator, contents_by_pair)


def create_tournament_request_email_snippets(
    brand: Brand,
    creator: User,
) -> bool:
    """Idempotently create default email snippets for tournament-request
    accept/reject decisions.

    Returns True if snippets were created, False if they already exist.
    """
    contents_by_pair = {
        (SNIPPET_NAME_REQUEST_ACCEPTED_BODY, 'en'): (
            '\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n'
            '\U0001f389 TOURNAMENT REQUEST ACCEPTED\n'
            '\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n'
            '\n'
            'Your tournament request "{request_name}" has been accepted.\n'
            '\n'
            'The admin team will now set up the tournament. You can follow '
            'its status on the "My tournament requests" page.\n'
            '\n'
            '{footer}'
        ),
        (SNIPPET_NAME_REQUEST_ACCEPTED_BODY, 'de'): (
            '\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n'
            '\U0001f389 TURNIERANFRAGE ANGENOMMEN\n'
            '\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n'
            '\n'
            'Deine Turnieranfrage "{request_name}" wurde angenommen.\n'
            '\n'
            'Das Admin-Team richtet das Turnier nun ein. Den Status kannst '
            'du auf der Seite "Meine Turnieranfragen" verfolgen.\n'
            '\n'
            '{footer}'
        ),
        (SNIPPET_NAME_REQUEST_ACCEPTED_SUBJECT, 'en'): (
            '[{request_name}] Your tournament request was accepted! \U0001f389'
        ),
        (SNIPPET_NAME_REQUEST_ACCEPTED_SUBJECT, 'de'): (
            '[{request_name}] Deine Turnieranfrage wurde angenommen! \U0001f389'
        ),
        (SNIPPET_NAME_REQUEST_REJECTED_BODY, 'en'): (
            '\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n'
            'TOURNAMENT REQUEST REJECTED\n'
            '\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n'
            '\n'
            'Your tournament request "{request_name}" has been rejected.\n'
            '\n'
            'Reason: {reason}\n'
            '\n'
            '{footer}'
        ),
        (SNIPPET_NAME_REQUEST_REJECTED_BODY, 'de'): (
            '\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n'
            'TURNIERANFRAGE ABGELEHNT\n'
            '\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n'
            '\n'
            'Deine Turnieranfrage "{request_name}" wurde abgelehnt.\n'
            '\n'
            'Grund: {reason}\n'
            '\n'
            '{footer}'
        ),
        (SNIPPET_NAME_REQUEST_REJECTED_SUBJECT, 'en'): (
            '[{request_name}] Your tournament request was rejected'
        ),
        (SNIPPET_NAME_REQUEST_REJECTED_SUBJECT, 'de'): (
            '[{request_name}] Deine Turnieranfrage wurde abgelehnt'
        ),
    }

    return _create_missing_snippets(brand, creator, contents_by_pair)


def email_templates_exist(brand: Brand) -> bool:
    """Check whether every required tournament-notification email
    snippet -- match-ready and request-decision, en and de -- exists
    for the brand. A single missing pair makes this False.
    """
    return not _find_missing_snippets(brand)
