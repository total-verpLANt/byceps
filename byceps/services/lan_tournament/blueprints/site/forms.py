"""
byceps.services.lan_tournament.blueprints.site.forms
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from flask_babel import lazy_gettext
from wtforms import (
    BooleanField,
    DateTimeLocalField,
    IntegerField,
    RadioField,
    StringField,
    TextAreaField,
)
from wtforms.validators import InputRequired, Length, Optional

from byceps.services.lan_tournament import tournament_request_domain_service
from byceps.services.lan_tournament.form_validators import SafeNumberRange
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.util.l10n import LocalizedForm


class SiteTeamCreateForm(LocalizedForm):
    name = StringField(
        lazy_gettext('Name'), [InputRequired(), Length(max=80)]
    )
    tag = StringField(lazy_gettext('Tag'), [Optional(), Length(max=20)])
    description = TextAreaField(
        lazy_gettext('Description'), [Optional(), Length(max=2000)]
    )
    join_code = StringField(
        lazy_gettext('Join code'), [Optional(), Length(max=80)]
    )


class SiteTeamUpdateForm(LocalizedForm):
    name = StringField(
        lazy_gettext('Name'), [InputRequired(), Length(max=80)]
    )
    tag = StringField(lazy_gettext('Tag'), [Optional(), Length(max=20)])
    description = TextAreaField(
        lazy_gettext('Description'), [Optional(), Length(max=2000)]
    )
    join_code = StringField(
        lazy_gettext('Join code'), [Optional(), Length(max=80)]
    )


class HighscoreSubmitForm(LocalizedForm):
    score = IntegerField(
        lazy_gettext('Score'),
        [InputRequired(), SafeNumberRange(min=0, max=999999999)],
    )
    note = StringField(
        lazy_gettext('Note'), [Optional(), Length(max=200)]
    )


class MatchCommentForm(LocalizedForm):
    comment = TextAreaField(
        lazy_gettext('Comment'),
        [InputRequired(), Length(max=1000)],
    )


class OrgaMatchUnconfirmForm(LocalizedForm):
    reason = TextAreaField(
        lazy_gettext('Reason'), [InputRequired(), Length(min=1, max=2000)]
    )


class OrgaMatchCorrectionForm(LocalizedForm):
    """Validate the reason and acknowledgement of a result correction."""

    reason = TextAreaField(
        lazy_gettext('Reason'), [InputRequired(), Length(min=1, max=2000)]
    )
    ack_critical = BooleanField(
        lazy_gettext('I understand the consequences'), [Optional()]
    )


_ELIMINATION_MODE_LABELS = {
    EliminationMode.SINGLE_ELIMINATION: lazy_gettext('Single Elimination'),
    EliminationMode.DOUBLE_ELIMINATION: lazy_gettext('Double Elimination'),
    EliminationMode.ROUND_ROBIN: lazy_gettext('Round Robin'),
    EliminationMode.NONE: lazy_gettext('None'),
}


def elimination_mode_label(mode: EliminationMode):
    """Return the translated label for an elimination mode.

    Public wrapper around `_ELIMINATION_MODE_LABELS`, for views/templates
    that need to show a frozen `elimination_mode` without going through
    `elimination_mode_options` (which only a live form builds).
    """
    return _ELIMINATION_MODE_LABELS[mode]


def _describe_elimination_mode_reason(reason: str):
    """Turn a domain-service reason code into a human-readable string.

    `allowed_elimination_modes` names a reason `only_<format>` when
    exactly one other game format accepts the mode, or
    `invalid_for_<format>` otherwise; both name a `GameFormat` member
    by its lower-cased `name`.
    """
    if reason.startswith('only_'):
        game_format = GameFormat[reason.removeprefix('only_').upper()]
        return lazy_gettext(
            'Only available for %(format)s', format=game_format.label
        )
    if reason.startswith('invalid_for_'):
        game_format = GameFormat[reason.removeprefix('invalid_for_').upper()]
        return lazy_gettext(
            'Not available for %(format)s', format=game_format.label
        )
    return reason


def _reasons_by_format(
    mode: EliminationMode,
    modes_by_format: dict[GameFormat, dict[EliminationMode, str | None]],
) -> dict[str, str | None]:
    """Map every `GameFormat` value to `mode`'s reason for that format.

    ``None`` marks `mode` as valid for that format. Powers each
    elimination-mode option's `data-reasons` JSON, so
    `lan_tournament_request.js` can re-disable/re-enable options after
    a client-side game-format switch, with no server round trip and no
    hardcoded copy of its own -- the reason text is gettext'd here,
    server-side, same as the currently-selected-format `reason` value
    below.
    """
    result: dict[str, str | None] = {}
    for fmt, modes in modes_by_format.items():
        reason = modes[mode]
        result[fmt.value] = (
            str(_describe_elimination_mode_reason(reason))
            if reason is not None
            else None
        )
    return result


class TournamentProposeForm(LocalizedForm):
    """Propose a new tournament, or edit one still in `submitted` status."""

    name = StringField(lazy_gettext('Name'), [InputRequired(), Length(max=80)])
    game = StringField(lazy_gettext('Game'), [InputRequired(), Length(max=80)])
    game_format = RadioField(lazy_gettext('Game format'), [InputRequired()])
    elimination_mode = RadioField(
        lazy_gettext('Elimination mode'), [InputRequired()]
    )
    team_size = IntegerField(
        lazy_gettext('Team size'),
        [InputRequired(), SafeNumberRange(min=1, max=64)],
    )
    participant_limit = IntegerField(
        lazy_gettext('Participant limit'),
        [
            InputRequired(),
            SafeNumberRange(
                min=2,
                max=tournament_request_domain_service.MAX_PARTICIPANT_LIMIT,
            ),
        ],
    )
    preferred_start_time = DateTimeLocalField(
        lazy_gettext('Preferred start'),
        validators=[
            InputRequired(),
            tournament_request_domain_service.year_in_range_validator,
        ],
    )
    preferred_end_time = DateTimeLocalField(
        lazy_gettext('Preferred end'),
        validators=[
            InputRequired(),
            tournament_request_domain_service.year_in_range_validator,
        ],
    )
    description = TextAreaField(
        lazy_gettext('Short description'), [InputRequired(), Length(max=2000)]
    )
    special_rules = TextAreaField(
        lazy_gettext('Special rules'), [Optional(), Length(max=2000)]
    )
    notes = TextAreaField(
        lazy_gettext('Notes'), [Optional(), Length(max=2000)]
    )
    desired_template = StringField(
        lazy_gettext('Desired template'), [Optional(), Length(max=200)]
    )

    # Populated by `set_format_choices`: one dict per elimination mode,
    # `{'value', 'label', 'disabled', 'reason', 'reasons'}`, in the
    # fixed order `allowed_elimination_modes` returns them. The
    # template renders every entry -- disabled ones struck through
    # with their reason shown, never omitted. `reasons` maps every
    # `GameFormat` value to that mode's reason for it (`None` when
    # valid), so `lan_tournament_request.js` can re-evaluate every
    # option after a client-side game-format switch.
    elimination_mode_options: list[dict[str, object]]

    def set_format_choices(self) -> None:
        """Populate `game_format` and `elimination_mode` choices.

        `elimination_mode.choices` always lists every mode, regardless
        of the currently selected game format, so a client-side format
        switch never posts a value WTForms would reject outright. The
        valid/invalid split for the *currently selected* format only
        drives which options `elimination_mode_options` marks disabled
        for the no-JS render -- `lan_tournament_request.js` re-derives
        that split for whichever format is actually selected in the
        browser from each option's `reasons` mapping, since a format
        switch after the initial render otherwise leaves the disabled
        state stale. Either way, the actual format/mode compatibility
        rule is enforced again in `validate_request_fields`.
        """
        self.game_format.choices = [
            (fmt.value, fmt.label) for fmt in GameFormat
        ]

        try:
            selected_format = GameFormat(self.game_format.data)
            has_selected_format = True
        except (ValueError, TypeError):
            # No/invalid format posted (e.g. a no-JS first submit):
            # don't guess ONE_V_ONE's restrictions onto every mode --
            # leave every option enabled instead. The real compatibility
            # rule still runs, server-side, in validate_request_fields.
            selected_format = GameFormat.ONE_V_ONE
            has_selected_format = False

        modes_by_format = {
            fmt: dict(
                tournament_request_domain_service.allowed_elimination_modes(fmt)
            )
            for fmt in GameFormat
        }

        self.elimination_mode.choices = []
        self.elimination_mode_options = []
        for mode, raw_reason in modes_by_format[selected_format].items():
            reason = raw_reason if has_selected_format else None
            label = _ELIMINATION_MODE_LABELS[mode]
            self.elimination_mode.choices.append((mode.value, label))
            self.elimination_mode_options.append(
                {
                    'value': mode.value,
                    'label': label,
                    'disabled': reason is not None,
                    'reason': (
                        _describe_elimination_mode_reason(reason)
                        if reason is not None
                        else None
                    ),
                    'reasons': _reasons_by_format(mode, modes_by_format),
                }
            )
