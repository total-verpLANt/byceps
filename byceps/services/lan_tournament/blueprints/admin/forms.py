import json

from flask_babel import lazy_gettext
from wtforms import (
    BooleanField,
    DateTimeLocalField,
    HiddenField,
    IntegerField,
    SelectField,
    StringField,
    TextAreaField,
)
from wtforms.validators import (
    InputRequired,
    Length,
    Optional,
    ValidationError,
)

from byceps.services.user import screen_name_validator, user_service
from byceps.util.l10n import LocalizedForm

from byceps.services.lan_tournament import tournament_request_domain_service
from byceps.services.lan_tournament.form_validators import SafeNumberRange
from byceps.services.lan_tournament.models.contestant_type import (
    ContestantType,
)
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.score_ordering import ScoreOrdering


def _get_contestant_type_choices() -> list[tuple[str, str]]:
    return [
        ('', lazy_gettext('– select –')),
        (ContestantType.SOLO.name, lazy_gettext('Solo')),
        (ContestantType.TEAM.name, lazy_gettext('Team')),
    ]


def _get_game_format_choices() -> list[tuple[str, str]]:
    return [
        ('', lazy_gettext('– select –')),
        (GameFormat.ONE_V_ONE.name, GameFormat.ONE_V_ONE.label),
        (GameFormat.FREE_FOR_ALL.name, GameFormat.FREE_FOR_ALL.label),
        (GameFormat.HIGHSCORE.name, GameFormat.HIGHSCORE.label),
    ]


def _get_elimination_mode_choices() -> list[tuple[str, str]]:
    return [
        ('', lazy_gettext('– select –')),
        (
            EliminationMode.SINGLE_ELIMINATION.name,
            lazy_gettext('Single Elimination'),
        ),
        (
            EliminationMode.DOUBLE_ELIMINATION.name,
            lazy_gettext('Double Elimination'),
        ),
        (EliminationMode.ROUND_ROBIN.name, lazy_gettext('Round Robin')),
        (EliminationMode.NONE.name, lazy_gettext('None')),
    ]


def _get_score_ordering_choices() -> list[tuple[str, str]]:
    return [
        ('', lazy_gettext('– select –')),
        (ScoreOrdering.HIGHER_IS_BETTER.name, lazy_gettext('Higher is better')),
        (ScoreOrdering.LOWER_IS_BETTER.name, lazy_gettext('Lower is better')),
    ]


class _BaseForm(LocalizedForm):
    name = StringField(lazy_gettext('Name'), [InputRequired(), Length(max=80)])
    game = StringField(lazy_gettext('Game'), [Optional(), Length(max=80)])
    description = TextAreaField(
        lazy_gettext('Description'), [Optional(), Length(max=10000)]
    )
    image_url = StringField(
        lazy_gettext('Image URL'), [Optional(), Length(max=256)]
    )
    ruleset = TextAreaField(
        lazy_gettext('Ruleset'), [Optional(), Length(max=10000)]
    )
    start_time = DateTimeLocalField(
        lazy_gettext('Start time'),
        validators=[
            Optional(),
            tournament_request_domain_service.year_in_range_validator,
        ],
    )
    contestant_type = SelectField(
        lazy_gettext('Contestant type'), validators=[Optional()]
    )
    game_format = SelectField(
        lazy_gettext('Game format'), validators=[Optional()]
    )
    elimination_mode = SelectField(
        lazy_gettext('Elimination mode'), validators=[Optional()]
    )
    score_ordering = SelectField(
        lazy_gettext('Score ordering'), validators=[Optional()]
    )
    min_players = IntegerField(lazy_gettext('Min. players'), [Optional()])
    max_players = IntegerField(lazy_gettext('Max. players'), [Optional()])
    min_teams = IntegerField(lazy_gettext('Min. teams'), [Optional()])
    max_teams = IntegerField(lazy_gettext('Max. teams'), [Optional()])
    min_players_in_team = IntegerField(
        lazy_gettext('Min. players per team'), [Optional()]
    )
    max_players_in_team = IntegerField(
        lazy_gettext('Max. players per team'), [Optional()]
    )
    point_table = StringField(
        lazy_gettext('Points by placement'),
        [Optional(), Length(max=500)],
    )
    group_size_min = IntegerField(
        lazy_gettext('Min. group size'),
        [Optional(), SafeNumberRange(min=2)],
    )
    group_size_max = IntegerField(
        lazy_gettext('Max. group size'),
        [Optional(), SafeNumberRange(min=2)],
    )
    advancement_count = IntegerField(
        lazy_gettext('Advance per group'),
        [Optional(), SafeNumberRange(min=1)],
    )
    points_carry_to_losers = BooleanField(
        lazy_gettext('Points carry to losers pool'),
    )

    def set_contestant_type_choices(self):
        self.contestant_type.choices = _get_contestant_type_choices()

    def set_game_format_choices(self):
        self.game_format.choices = _get_game_format_choices()

    def set_elimination_mode_choices(self):
        self.elimination_mode.choices = _get_elimination_mode_choices()

    def set_score_ordering_choices(self):
        self.score_ordering.choices = _get_score_ordering_choices()


class TournamentCreateForm(_BaseForm):
    # Set by `create_form` when opened from an accepted tournament
    # request (`?from_request=<uuid>`); read back by `create`, which
    # passes it to `tournament_service.create_tournament` to link the
    # tournament to that request in the same transaction.
    from_request_id = HiddenField()


class TournamentUpdateForm(_BaseForm):
    pass


class TeamCreateForm(LocalizedForm):
    captain = StringField(
        lazy_gettext('Captain'),
        [
            InputRequired(),
            Length(
                min=screen_name_validator.MIN_LENGTH,
                max=screen_name_validator.MAX_LENGTH,
            ),
        ],
    )
    name = StringField(lazy_gettext('Name'), [InputRequired(), Length(max=80)])
    tag = StringField(lazy_gettext('Tag'), [Optional(), Length(max=20)])
    description = TextAreaField(
        lazy_gettext('Description'), [Optional(), Length(max=2000)]
    )
    image_url = StringField(
        lazy_gettext('Image URL'), [Optional(), Length(max=256)]
    )
    join_code = StringField(
        lazy_gettext('Join code'), [Optional(), Length(max=80)]
    )

    @staticmethod
    def validate_captain(form, field):
        screen_name = field.data.strip()

        if not screen_name_validator.contains_only_valid_chars(screen_name):
            raise ValidationError(lazy_gettext('Contains invalid characters.'))

        user = user_service.find_user_by_screen_name(screen_name)
        if user is None:
            raise ValidationError(lazy_gettext('Unknown username'))

        field.data = screen_name  # keep string for re-render
        form.captain_user = user  # stash resolved User on the form


class AddParticipantForm(LocalizedForm):
    screen_name = StringField(
        lazy_gettext('Screen name'),
        [
            InputRequired(),
            Length(
                min=screen_name_validator.MIN_LENGTH,
                max=screen_name_validator.MAX_LENGTH,
            ),
        ],
    )

    @staticmethod
    def validate_screen_name(form, field):
        screen_name = field.data.strip()

        if not screen_name_validator.contains_only_valid_chars(screen_name):
            raise ValidationError(lazy_gettext('Contains invalid characters.'))

        user = user_service.find_user_by_screen_name(screen_name)
        if user is None:
            raise ValidationError(lazy_gettext('Unknown username'))

        field.data = screen_name  # keep string for re-render
        form.user = user  # stash resolved User on the form


class TeamUpdateForm(LocalizedForm):
    name = StringField(lazy_gettext('Name'), [InputRequired(), Length(max=80)])
    tag = StringField(lazy_gettext('Tag'), [Optional(), Length(max=20)])
    description = TextAreaField(
        lazy_gettext('Description'), [Optional(), Length(max=2000)]
    )
    image_url = StringField(
        lazy_gettext('Image URL'), [Optional(), Length(max=256)]
    )
    join_code = StringField(
        lazy_gettext('Join code'), [Optional(), Length(max=80)]
    )


class TransferCaptainForm(LocalizedForm):
    new_captain = SelectField(lazy_gettext('New captain'), [InputRequired()])


class AddTeamMemberForm(LocalizedForm):
    screen_name = StringField(
        lazy_gettext('Screen name'),
        [
            InputRequired(),
            Length(
                min=screen_name_validator.MIN_LENGTH,
                max=screen_name_validator.MAX_LENGTH,
            ),
        ],
    )

    @staticmethod
    def validate_screen_name(form, field):
        screen_name = field.data.strip()

        if not screen_name_validator.contains_only_valid_chars(screen_name):
            raise ValidationError(lazy_gettext('Contains invalid characters.'))

        user = user_service.find_user_by_screen_name(screen_name)
        if user is None:
            raise ValidationError(lazy_gettext('Unknown username'))

        field.data = screen_name
        form.user = user


class TournamentOrgaAssignForm(LocalizedForm):
    screen_name = StringField(
        lazy_gettext('Username'), [InputRequired(), Length(max=80)]
    )
    duties = StringField(lazy_gettext('Duties'), [Optional(), Length(max=200)])


class MatchCorrectionForm(LocalizedForm):
    """Validate the non-score fields of a result correction.

    The view parses the `corrected_score_<contestant key>` fields itself.
    """

    reason = TextAreaField(
        lazy_gettext('Reason'), validators=[InputRequired(), Length(max=2000)]
    )
    ack_critical = BooleanField(
        lazy_gettext(
            'I acknowledge that the confirmed downstream matches '
            'listed above will be retracted and their scores cleared.'
        ),
        validators=[Optional()],
    )


class MatchUnconfirmForm(LocalizedForm):
    reason = TextAreaField(
        lazy_gettext('Reason'), validators=[InputRequired(), Length(max=2000)]
    )


class TournamentRequestRejectForm(LocalizedForm):
    reason = TextAreaField(
        lazy_gettext('Reason'), [InputRequired(), Length(min=1, max=2000)]
    )

    @staticmethod
    def validate_reason(form, field):
        """Mirror `tournament_request_service.reject_request`'s check.

        Without this, a reason containing a disallowed control
        character (NUL foremost) only fails once it reaches the
        service, at which point the view's form-error re-render path
        is the one preserving the submitted text -- so the same check
        belongs here too, with the identical message, rather than the
        request only failing downstream.
        """
        if field.data is not None and (
            tournament_request_domain_service.contains_disallowed_control_char(
                field.data
            )
        ):
            raise ValidationError(
                lazy_gettext('The reason must not contain control characters.')
            )


_REQUEST_ELIMINATION_MODE_LABELS = {
    EliminationMode.SINGLE_ELIMINATION: lazy_gettext('Single Elimination'),
    EliminationMode.DOUBLE_ELIMINATION: lazy_gettext('Double Elimination'),
    EliminationMode.ROUND_ROBIN: lazy_gettext('Round Robin'),
    EliminationMode.NONE: lazy_gettext('None'),
}


def _describe_request_elimination_mode_reason(reason: str):
    """Turn an `allowed_elimination_modes` reason code into text.

    Mirrors the identically-named helper in `blueprints/site/forms.py`
    byte-for-byte on the msgids, so the two surfaces share one catalog
    entry per reason instead of two.
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

    Mirrors the identically-named helper in `blueprints/site/forms.py`.
    ``None`` marks `mode` as valid for that format. Serialized to the
    `<option>`'s `data-reasons` JSON so `lan_tournament_request.js` can
    re-disable/re-enable options after a client-side game-format
    switch, with no server round trip and no hardcoded copy of its own.
    """
    result: dict[str, str | None] = {}
    for fmt, modes in modes_by_format.items():
        reason = modes[mode]
        result[fmt.value] = (
            str(_describe_request_elimination_mode_reason(reason))
            if reason is not None
            else None
        )
    return result


class TournamentRequestUpdateForm(LocalizedForm):
    """Edit an open tournament request. Admin surface.

    Same field set as the site's `TournamentProposeForm`
    (`blueprints/site/forms.py`). Compromise C2 is the one deliberate
    difference: `game_format`/`elimination_mode` render as
    `SelectField`s here rather than the site's `RadioField`s, per the
    admin design. Invalid elimination modes are rendered `disabled`,
    with the reason folded into the option label -- decision D10:
    never hidden.
    """

    name = StringField(lazy_gettext('Name'), [InputRequired(), Length(max=80)])
    game = StringField(lazy_gettext('Game'), [InputRequired(), Length(max=80)])
    game_format = SelectField(lazy_gettext('Game format'), [InputRequired()])
    elimination_mode = SelectField(
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
    notes = TextAreaField(lazy_gettext('Notes'), [Optional(), Length(max=2000)])
    desired_template = StringField(
        lazy_gettext('Desired template'), [Optional(), Length(max=200)]
    )

    def set_format_choices(self) -> None:
        """Populate `game_format` and `elimination_mode` choices.

        Mirrors `TournamentProposeForm.set_format_choices`: every
        elimination mode is listed regardless of the selected game
        format, so a stale POST never trips a bare "invalid choice"
        error; only the *rendering* marks the currently-invalid modes
        disabled. Every `<option>` also carries a `data-reasons` JSON
        mapping (every `GameFormat` value to that mode's reason, or
        `None`) and a `data-label-base` (the plain label, no reason
        suffix), so `lan_tournament_request.js` can rewrite the
        disabled state and option text after a client-side game-format
        switch. `validate_request_fields`/`is_valid_combination`
        (called again in the view) is the real enforcement point.
        """
        self.game_format.choices = [
            (fmt.value, fmt.label) for fmt in GameFormat
        ]

        try:
            selected_format = GameFormat(self.game_format.data)
        except (ValueError, TypeError):
            selected_format = GameFormat.ONE_V_ONE

        modes_by_format = {
            fmt: dict(
                tournament_request_domain_service.allowed_elimination_modes(fmt)
            )
            for fmt in GameFormat
        }

        choices: list[tuple[str, str, dict[str, object]]] = []
        for mode, reason in modes_by_format[selected_format].items():
            label = _REQUEST_ELIMINATION_MODE_LABELS[mode]
            label_text = str(label)
            render_kw: dict[str, object] = {
                'data_label_base': label_text,
                'data_reasons': json.dumps(
                    _reasons_by_format(mode, modes_by_format)
                ),
            }
            if reason is None:
                choices.append((mode.value, label_text, render_kw))
            else:
                reason_text = _describe_request_elimination_mode_reason(reason)
                render_kw['disabled'] = True
                choices.append(
                    (
                        mode.value,
                        f'{label_text} ({reason_text})',
                        render_kw,
                    )
                )
        self.elimination_mode.choices = choices


class HighscoreSubmitForm(LocalizedForm):
    contestant = SelectField(lazy_gettext('Contestant'))
    score = IntegerField(
        lazy_gettext('Score'),
        validators=[InputRequired(), SafeNumberRange(min=0)],
    )
    note = StringField(lazy_gettext('Note'))
