"""
byceps.services.lan_tournament.form_validators
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

import math

from wtforms.validators import NumberRange, ValidationError


def _is_nan(data: float) -> bool:
    """Return whether `data` is NaN, without crashing on a huge int.

    `math.isnan` converts its argument through `float()` first; for an
    `int` with magnitude >= 2**1024 (~309+ decimal digits) that raises
    `OverflowError: int too large to convert to float` instead of
    returning a bool. An `int` can never be NaN, so the float
    conversion is skipped for it entirely; a `float` goes through the
    original check unchanged.
    """
    if isinstance(data, int):
        return False

    return math.isnan(data)


class SafeNumberRange(NumberRange):
    """`NumberRange` that tolerates an out-of-float-range int.

    Every `IntegerField` in this module's `blueprints/site/forms.py`
    and `blueprints/admin/forms.py` uses this in place of
    `wtforms.validators.NumberRange`. `IntegerField` parses ints up to
    4300 digits, and stock `NumberRange.__call__` calls `math.isnan`
    unconditionally on the parsed value, which raises for a 309+ digit
    int rather than returning a bool -- turning any such POST
    (team_size, participant_limit, group_size_max, ...) into an
    unhandled 500 instead of the intended range-validation error.
    Behavior and message text are otherwise identical to `NumberRange`;
    `.min`/`.max`/`field_flags` are inherited unchanged, so rendered
    `min`/`max` widget attributes and template `selectattr` checks
    keep working.
    """

    def __call__(self, form, field):
        data = field.data
        if (
            data is not None
            and not _is_nan(data)
            and (self.min is None or data >= self.min)
            and (self.max is None or data <= self.max)
        ):
            return

        if self.message is not None:
            message = self.message
        elif self.max is None:
            message = field.gettext('Number must be at least %(min)s.')
        elif self.min is None:
            message = field.gettext('Number must be at most %(max)s.')
        else:
            message = field.gettext(
                'Number must be between %(min)s and %(max)s.'
            )

        raise ValidationError(message % dict(min=self.min, max=self.max))
