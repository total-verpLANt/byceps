"""
byceps.services.lan_tournament.blueprints.dashboard_forms
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Shared pin and acknowledgement forms of the orga dashboard.

The caller verifies the CSRF token with `validate_dashboard_csrf` before it
validates a form, so a missing token answers `csrf_invalid`, not a field
error. Every hidden field is untrusted and bounded here. Nothing in these
forms authorizes anything.
"""

import re
import unicodedata
from uuid import UUID

from flask_babel import lazy_gettext
from wtforms import HiddenField, TextAreaField
from wtforms.validators import ValidationError

from byceps.services.lan_tournament.models.operational_timing import (
    MatchDueEpisodeID,
)
from byceps.util.l10n import LocalizedForm


MAX_COMMENT_LENGTH = 500
MAX_REVISION = 2_147_483_647  # PostgreSQL INTEGER, as the revision columns
MAX_RETURN_LENGTH = 1024

_REVISION_PATTERN = re.compile(r'[0-9]{1,10}')
_EPISODE_ID_PATTERN = re.compile(
    r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}'
    r'-[0-9a-fA-F]{12}'
)

_ALLOWED_CONTROL_CHARACTERS = frozenset('\n\t')
_REFUSED_CATEGORIES = frozenset({'Cc', 'Cs', 'Zl', 'Zp'})
# Bidirectional overrides and isolates: a shared comment could reorder what
# other orgas read.
_BIDI_CONTROLS = frozenset(
    '\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069'
)

_INVALID_FORM_DATA = lazy_gettext('Invalid form data.')


def parse_dashboard_revision(raw: object) -> int:
    """Accept only a bounded unsigned decimal that fits a revision column."""
    if type(raw) is int:
        value = raw
    elif isinstance(raw, str) and _REVISION_PATTERN.fullmatch(raw):
        value = int(raw)
    else:
        raise ValueError('invalid_dashboard_revision')

    if not 0 <= value <= MAX_REVISION:
        raise ValueError('invalid_dashboard_revision')

    return value


def parse_dashboard_episode_id(raw: object) -> MatchDueEpisodeID:
    """Accept only a UUID in its canonical hyphenated form."""
    if isinstance(raw, UUID):
        return MatchDueEpisodeID(raw)

    if not isinstance(raw, str) or not _EPISODE_ID_PATTERN.fullmatch(raw):
        raise ValueError('invalid_dashboard_episode_id')

    return MatchDueEpisodeID(UUID(raw))


def parse_dashboard_pin_state(raw: object) -> bool:
    """Accept only `true` or `false`; there is no default and no toggle."""
    if raw is True or raw == 'true':
        return True

    if raw is False or raw == 'false':
        return False

    raise ValueError('invalid_dashboard_pin_state')


class _SingleValueHiddenField(HiddenField):
    """Hidden field holding exactly one value, parsed by `parse`."""

    def parse(self, raw: object):
        raise NotImplementedError

    def process_data(self, value):
        self.data = None
        if value is not None:
            self._set(value)

    def process_formdata(self, valuelist):
        self.data = None
        if len(valuelist) != 1:
            raise ValueError(_INVALID_FORM_DATA)
        self._set(valuelist[0])

    def pre_validate(self, form):
        if self.data is None and not self.process_errors:
            raise ValidationError(_INVALID_FORM_DATA)

    def _set(self, raw):
        try:
            self.data = self.parse(raw)
        except ValueError:
            raise ValueError(_INVALID_FORM_DATA) from None


class RevisionField(_SingleValueHiddenField):
    def parse(self, raw):
        return parse_dashboard_revision(raw)


class EpisodeIDField(_SingleValueHiddenField):
    def parse(self, raw):
        return parse_dashboard_episode_id(raw)


class PinStateField(_SingleValueHiddenField):
    def parse(self, raw):
        return parse_dashboard_pin_state(raw)

    def _value(self):
        if self.data is None:
            return ''
        return 'true' if self.data else 'false'


class ReturnTargetField(HiddenField):
    """Raw list context to return to; the caller allowlists it.

    Too long a value is dropped, not refused: an unusable `return` falls
    back to the default list and must not cost the user the action.
    """

    def process_data(self, value):
        self.data = _bounded_return_target(value)

    def process_formdata(self, valuelist):
        self.data = _bounded_return_target(valuelist[0] if valuelist else None)


class CommentField(TextAreaField):
    """Optional plain text. Newlines are normalized, blank becomes `None`."""

    def process_data(self, value):
        self.data = None
        if value is not None:
            self.data = self._normalize(value)

    def process_formdata(self, valuelist):
        self.data = None
        if len(valuelist) > 1:
            raise ValueError(_INVALID_FORM_DATA)
        if valuelist:
            self.data = self._normalize(valuelist[0])

    @staticmethod
    def _normalize(raw):
        if not isinstance(raw, str):
            raise ValueError(_INVALID_FORM_DATA)
        text = raw.replace('\r\n', '\n').replace('\r', '\n').strip()
        return text or None


def validate_comment(form, field) -> None:
    """Refuse text above the bound or with characters beyond plain text.

    Unlike `Length`, this adds no `maxlength` flag, so no widget renders the
    attribute and a pasted text is never cut silently (design R16).
    """
    text = field.data
    if text is None:
        return

    if len(text) > MAX_COMMENT_LENGTH:
        raise ValidationError(
            lazy_gettext(
                'Comment is too long: %(count)d of at most 500 characters.'
                ' Nothing was saved.',
                count=len(text),
            )
        )

    if not _is_plain_text(text):
        raise ValidationError(
            lazy_gettext(
                'Comment contains characters that are not allowed in plain'
                ' text. Nothing was saved.'
            )
        )


class _DashboardActionForm(LocalizedForm):
    csrf_token = HiddenField(lazy_gettext('CSRF token'))
    revision = RevisionField(lazy_gettext('Revision'))
    return_to = ReturnTargetField(lazy_gettext('Return target'), name='return')


class DashboardPinForm(_DashboardActionForm):
    """Set the shared pin of a match to an explicit state.

    `revision` is the pin revision the form was rendered from.
    """

    pinned = PinStateField(lazy_gettext('Pin state'))


class DashboardAcknowledgementForm(_DashboardActionForm):
    """Record a check of one due episode, with an optional comment.

    `revision` is the acknowledgement revision of `episode` the form was
    rendered from.
    """

    episode = EpisodeIDField(lazy_gettext('Episode'))
    comment = CommentField(
        lazy_gettext('Comment (optional)'), [validate_comment]
    )


def _bounded_return_target(raw: object) -> str | None:
    if isinstance(raw, str) and len(raw) <= MAX_RETURN_LENGTH:
        return raw

    return None


def _is_plain_text(text: str) -> bool:
    return not any(
        character in _BIDI_CONTROLS
        or (
            unicodedata.category(character) in _REFUSED_CATEGORIES
            and character not in _ALLOWED_CONTROL_CHARACTERS
        )
        for character in text
    )
