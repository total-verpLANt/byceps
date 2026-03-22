"""
byceps.services.pizza_delivery.pizza_delivery_email_service
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Email notifications for pizza delivery entries.

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
from byceps.services.email.models import Message
from byceps.services.party import party_service
from byceps.services.party.models import PartyID
from byceps.services.snippet import snippet_service
from byceps.services.snippet.models import SnippetScope
from byceps.services.user import user_service
from byceps.services.user.models import User, UserID
from byceps.util.l10n import get_default_locale

from .models import PizzaDeliveryEntry


log = structlog.get_logger()


SNIPPET_NAME_BODY = 'email_pizza_delivery_body'
SNIPPET_NAME_SUBJECT = 'email_pizza_delivery_subject'


def send_notification(
    entry: PizzaDeliveryEntry,
    party_id: PartyID,
) -> None:
    """Send an email notification to the linked user that their pizza is ready.

    Only fires when ``entry.user_id`` is set.  Fails gracefully: snippet
    or user-data errors are logged but never propagate as exceptions.
    """
    if entry.user_id is None:
        return

    # Resolve party -> brand -> email config.
    party = party_service.get_party(party_id)
    brand = brand_service.get_brand(party.brand_id)
    email_config = email_config_service.get_config(party.brand_id)

    # Resolve user email address.
    email_address = user_service.find_email_address(entry.user_id)
    if email_address is None:
        log.warning(
            'User has no email address, skipping pizza delivery notification',
            user_id=str(entry.user_id),
        )
        return

    locale = user_service.find_locale(entry.user_id) or get_default_locale()
    language_code = locale.language

    # Fetch snippet body template.
    scope = SnippetScope.for_brand(brand.id)
    body_result = snippet_service.get_snippet_body(
        scope, SNIPPET_NAME_BODY, language_code
    )
    if body_result.is_err():
        log.error(
            'Pizza delivery email body snippet not found, skipping',
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
            'Pizza delivery email subject snippet not found, skipping',
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
            'Email footer not found, skipping pizza delivery notification',
            brand_id=str(brand.id),
            language_code=language_code,
        )
        return

    footer = footer_result.unwrap()

    # Escape curly braces in user-controlled values so they cannot
    # interfere with str.format() placeholders (format string injection).
    def _esc(value: str) -> str:
        return value.replace('{', '{{').replace('}', '}}')

    safe_number = _esc(entry.number)
    safe_party_name = _esc(party.title)

    try:
        body = body_template.format(
            number=safe_number,
            party_name=safe_party_name,
            footer=footer,
        )

        subject = subject_template.format(
            number=safe_number,
            party_name=safe_party_name,
        )
    except (KeyError, ValueError) as exc:
        log.error(
            'Failed to format pizza delivery email template, skipping',
            user_id=str(entry.user_id),
            error=str(exc),
        )
        return

    message = Message(
        sender=email_config.sender,
        recipients=[email_address],
        subject=subject,
        body=body,
    )
    email_service.enqueue_message(message)


# -- snippet setup ---------------------------------------------------


def create_pizza_delivery_email_snippets(
    brand: Brand,
    creator: User,
) -> bool:
    """Idempotently create default email snippets for pizza delivery notifications.

    Returns True if snippets were created, False if they already exist.
    """
    scope = SnippetScope.for_brand(brand.id)

    language_codes_and_bodies = [
        (
            'en',
            (
                'Your pizza (order number: {number}) is ready for pickup'
                ' at {party_name}!\n'
                '\n'
                '{footer}'
            ),
        ),
        (
            'de',
            (
                'Deine Pizza (Bestellnummer: {number}) ist abholbereit'
                ' bei {party_name}!\n'
                '\n'
                '{footer}'
            ),
        ),
    ]

    language_codes_and_subjects = [
        (
            'en',
            '[{party_name}] Your pizza is ready! ({number})',
        ),
        (
            'de',
            '[{party_name}] Deine Pizza ist da! ({number})',
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


def email_templates_exist(brand: Brand) -> bool:
    """Check whether pizza delivery email snippets exist for the brand."""
    scope = SnippetScope.for_brand(brand.id)
    existing = snippet_service.find_current_version_of_snippet_with_name(
        scope, SNIPPET_NAME_BODY, 'en'
    )
    return existing is not None
