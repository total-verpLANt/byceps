"""
byceps.services.lan_tournament.blueprints.site.forms
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from flask_babel import gettext, lazy_gettext
from wtforms import (
    BooleanField,
    DateTimeLocalField,
    IntegerField,
    RadioField,
    StringField,
    TextAreaField,
)
from wtforms.validators import (
    InputRequired,
    Length,
    Optional,
    StopValidation,
    ValidationError,
)

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


_REQUEST_MODE_LABELS = {
    EliminationMode.SINGLE_ELIMINATION: lazy_gettext('Single knockout'),
    EliminationMode.DOUBLE_ELIMINATION: lazy_gettext('Double knockout'),
    EliminationMode.ROUND_ROBIN: lazy_gettext('Everyone plays everyone'),
    EliminationMode.NONE: lazy_gettext('No knockout'),
}


def request_mode_label(mode: EliminationMode) -> str:
    """Return the request flow's label for an elimination mode."""
    return str(_REQUEST_MODE_LABELS[mode])


def _describe_elimination_mode_reason(reason: str):
    """Turn a domain-service reason code into a readable string."""
    if reason.startswith('only_'):
        game_format = GameFormat[reason.removeprefix('only_').upper()]
        return lazy_gettext(
            'only %(format)s', format=game_format_label(game_format)
        )
    if reason.startswith('invalid_for_'):
        game_format = GameFormat[reason.removeprefix('invalid_for_').upper()]
        return lazy_gettext(
            'not with %(format)s', format=game_format_label(game_format)
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


_REQUIRED_FIELD_MESSAGE = lazy_gettext('Required field')


class _RequiredStripped:
    """Reject empty or whitespace-only input."""

    field_flags = {'required': True}

    def __call__(self, form, field) -> None:
        if not (field.data or '').strip():
            raise StopValidation(_REQUIRED_FIELD_MESSAGE)


_required_stripped = _RequiredStripped()


class _ParticipantLimitRange(SafeNumberRange):
    """`SafeNumberRange` with a message that depends on which bound
    failed.

    Below `min`, the message names the real minimum. Above `max`, the
    message must never repeat `MAX_PARTICIPANT_LIMIT` -- that number
    is only this form's technical ceiling; the real upper bound is the
    party's ticket capacity, which `tournament_request_domain_service`
    enforces separately with its own message. One validator object
    still carries `.min`/`.max` (inherited unchanged), which
    `propose_form.html` reads via `selectattr('max', 'defined')`.
    """

    def __init__(
        self, *, min=None, max=None, min_message=None, max_message=None
    ):
        super().__init__(min=min, max=max)
        self.min_message = min_message
        self.max_message = max_message

    def __call__(self, form, field):
        data = field.data
        if data is None:
            # `IntegerField` already reports its own "Not a valid
            # integer value." for non-integer input (`2e1`, `abc`,
            # `2.5`); do not also raise a range message here, which
            # would repeat `.max` (1024) for a value that never
            # reached a numeric comparison.
            return
        if self.min is not None and data < self.min:
            raise ValidationError(
                self.min_message % dict(min=self.min, max=self.max)
            )
        if self.max is not None and data > self.max:
            raise ValidationError(
                self.max_message % dict(min=self.min, max=self.max)
            )


_GAME_FORMAT_METADATA = {
    GameFormat.ONE_V_ONE: (
        lazy_gettext('One on one'),
        lazy_gettext('Duel'),
    ),
    GameFormat.FREE_FOR_ALL: (
        lazy_gettext('Free-for-all'),
        lazy_gettext('Free-for-All'),
    ),
    GameFormat.HIGHSCORE: (
        lazy_gettext('Highscore'),
        # Deliberately not the shared `Leaderboard` msgid (nav tab,
        # already "Rangliste"): the draft's subtitle here is
        # "Bestenliste", a different word for a different UI spot.
        lazy_gettext('Ranking'),
    ),
}


def game_format_label(fmt: GameFormat) -> str:
    """Return the label of a game format."""
    return str(_GAME_FORMAT_METADATA[fmt][0])


_ELIMINATION_MODE_DESCRIPTIONS = {
    EliminationMode.SINGLE_ELIMINATION: lazy_gettext('Whoever loses is out.'),
    EliminationMode.DOUBLE_ELIMINATION: lazy_gettext(
        'A second chance in the losers bracket.'
    ),
    EliminationMode.ROUND_ROBIN: lazy_gettext(
        'League, everyone plays everyone.'
    ),
    EliminationMode.NONE: lazy_gettext('Leaderboard only, no rounds.'),
}


_END_BEFORE_START_MSGID = 'End is before the start (%(time)s)'


class TournamentProposeForm(LocalizedForm):
    """Propose a new tournament, or edit one still in `submitted` status."""

    name = StringField(
        lazy_gettext('Tournament name'), [_required_stripped, Length(max=80)]
    )
    game = StringField(
        lazy_gettext('Game'), [_required_stripped, Length(max=80)]
    )
    game_format = RadioField(
        lazy_gettext('Game format'),
        [InputRequired(message=_REQUIRED_FIELD_MESSAGE)],
    )
    elimination_mode = RadioField(
        lazy_gettext('Tournament mode'),
        [InputRequired(message=_REQUIRED_FIELD_MESSAGE)],
    )
    team_size = IntegerField(
        lazy_gettext('Team size'),
        [
            InputRequired(message=_REQUIRED_FIELD_MESSAGE),
            SafeNumberRange(min=1, max=64),
        ],
    )
    participant_limit = IntegerField(
        lazy_gettext('Participant limit'),
        [
            InputRequired(message=_REQUIRED_FIELD_MESSAGE),
            _ParticipantLimitRange(
                min=2,
                max=tournament_request_domain_service.MAX_PARTICIPANT_LIMIT,
                min_message=lazy_gettext('Enter at least %(min)s.'),
                max_message=lazy_gettext('This limit is too high.'),
            ),
        ],
    )
    preferred_start_time = DateTimeLocalField(
        lazy_gettext('Preferred start'),
        validators=[
            InputRequired(message=_REQUIRED_FIELD_MESSAGE),
            tournament_request_domain_service.year_in_range_validator,
        ],
    )
    preferred_end_time = DateTimeLocalField(
        lazy_gettext('Preferred end'),
        validators=[
            InputRequired(message=_REQUIRED_FIELD_MESSAGE),
            tournament_request_domain_service.year_in_range_validator,
        ],
    )
    description = TextAreaField(
        lazy_gettext('Short description'),
        [_required_stripped, Length(max=2000)],
    )
    special_rules = TextAreaField(
        lazy_gettext('Special rules'), [Optional(), Length(max=2000)]
    )
    notes = TextAreaField(
        lazy_gettext('Notes for the orga'),
        [Optional(), Length(max=2000)],
        id='request_notes',
    )
    desired_template = StringField(
        lazy_gettext('Desired template'), [Optional(), Length(max=200)]
    )

    def validate_preferred_end_time(self, field) -> None:
        """Reject an end time before the start time."""
        start = self.preferred_start_time.data
        if field.data is not None and start is not None and field.data < start:
            raise ValidationError(
                gettext(
                    _END_BEFORE_START_MSGID,
                    time=start.strftime('%H:%M'),
                )
            )

    def error_summary(self) -> list[tuple[str, str, str]]:
        """Map each field error to the draft's short summary phrase."""
        required_field_text = str(_REQUIRED_FIELD_MESSAGE)
        too_small_prefix = str(
            gettext('Enter at least %(min)s.', min=2)
        ).rstrip('.')
        before_start_prefix = str(gettext(_END_BEFORE_START_MSGID)).split(
            '%(time)s'
        )[0]

        summary: list[tuple[str, str, str]] = []
        for name, errors in self.errors.items():
            field = self[name]
            for error in errors:
                error_text = str(error)
                if error_text == required_field_text:
                    phrase = str(gettext('is missing'))
                elif name == 'participant_limit' and error_text.startswith(
                    too_small_prefix
                ):
                    phrase = str(gettext('is too small'))
                elif name == 'preferred_end_time' and error_text.startswith(
                    before_start_prefix
                ):
                    phrase = str(gettext('is before the start'))
                else:
                    phrase = error_text
                summary.append((field.id, str(field.label.text), phrase))
        return summary

    # Populated by `set_format_choices`: one dict per elimination mode,
    # `{'value', 'label', 'disabled', 'reason', 'reasons'}`, in the
    # fixed order `allowed_elimination_modes` returns them. The
    # template renders every entry -- disabled ones struck through
    # with their reason shown, never omitted. `reasons` maps every
    # `GameFormat` value to that mode's reason for it (`None` when
    # valid), so `lan_tournament_request.js` can re-evaluate every
    # option after a client-side game-format switch.
    elimination_mode_options: list[dict[str, object]]

    # Populated by `set_format_choices`: one dict per `GameFormat`,
    # `{'value', 'label', 'subtitle'}`, in `GameFormat` enum order.
    game_format_options: list[dict[str, object]]

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
        self.game_format_options = [
            {
                'value': fmt.value,
                'label': label,
                'subtitle': subtitle,
            }
            for fmt, (label, subtitle) in _GAME_FORMAT_METADATA.items()
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
            label = request_mode_label(mode)
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
                    'description': _ELIMINATION_MODE_DESCRIPTIONS[mode],
                }
            )
