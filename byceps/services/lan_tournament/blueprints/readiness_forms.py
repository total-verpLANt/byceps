"""Shared native readiness forms. CSRF validation belongs to the caller."""

import re

from flask_babel import lazy_gettext
from wtforms import HiddenField
from wtforms.validators import InputRequired, Length, ValidationError

from byceps.util.l10n import LocalizedForm


def parse_readiness_revision(raw) -> int:
    """Accept only bounded unsigned decimal values fitting PostgreSQL BIGINT."""
    if type(raw) is int:
        value = raw
    elif isinstance(raw, str) and re.fullmatch(r'[0-9]{1,19}', raw):
        value = int(raw)
    else:
        raise ValueError('invalid_readiness_revision')
    if not 0 <= value <= 9_223_372_036_854_775_807:
        raise ValueError('invalid_readiness_revision')
    return value


class ReadinessRevisionField(HiddenField):
    def process_data(self, value):
        # WTForms retains ValueError as a process error and still validates.
        self.data = None
        if value is not None:
            self.data = parse_readiness_revision(value)

    def process_formdata(self, valuelist):
        if len(valuelist) != 1:
            raise ValueError('invalid_readiness_revision')
        self.data = parse_readiness_revision(valuelist[0])

    def pre_validate(self, form):
        try:
            parse_readiness_revision(self.data)
        except ValueError:
            raise ValidationError(lazy_gettext('Invalid form data.')) from None


class _ReadinessRevisionForm(LocalizedForm):
    csrf_token = HiddenField(
        lazy_gettext('CSRF token'), [InputRequired(), Length(max=43)]
    )
    expected_pairing_generation = ReadinessRevisionField(
        lazy_gettext('Pairing generation')
    )
    expected_readiness_revision = ReadinessRevisionField(
        lazy_gettext('Readiness revision')
    )


class MatchReadyClaimForm(_ReadinessRevisionForm):
    side = HiddenField(lazy_gettext('Side'), [InputRequired(), Length(max=1)])

    def validate_side(self, field):
        if field.data not in {'a', 'b'}:
            raise ValidationError(lazy_gettext('Invalid match side.'))


class MatchReadyRevokeForm(MatchReadyClaimForm):
    """Same guarded fields as a claim; un-ready needs no reason."""
