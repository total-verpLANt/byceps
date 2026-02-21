from flask_babel import lazy_gettext
from wtforms import (
    DateTimeLocalField,
    IntegerField,
    SelectField,
    StringField,
    TextAreaField,
)
from wtforms.validators import InputRequired, Length, Optional, ValidationError

from byceps.services.user import screen_name_validator, user_service
from byceps.util.l10n import LocalizedForm

from byceps.services.lan_tournament.models.contestant_type import (
    ContestantType,
)
from byceps.services.lan_tournament.models.tournament_mode import (
    TournamentMode,
)


def _get_contestant_type_choices() -> list[tuple[str, str]]:
    return [
        ('', lazy_gettext('– select –')),
        (ContestantType.SOLO.name, lazy_gettext('Solo')),
        (ContestantType.TEAM.name, lazy_gettext('Team')),
    ]


def _get_tournament_mode_choices() -> list[tuple[str, str]]:
    return [
        ('', lazy_gettext('– select –')),
        (
            TournamentMode.SINGLE_ELIMINATION.name,
            lazy_gettext('Single elimination'),
        ),
        (
            TournamentMode.DOUBLE_ELIMINATION.name,
            lazy_gettext('Double elimination'),
        ),
        (TournamentMode.ROUND_ROBIN.name, lazy_gettext('Round robin')),
        (TournamentMode.HIGHSCORE.name, lazy_gettext('Highscore')),
    ]


class _BaseForm(LocalizedForm):
    name = StringField(lazy_gettext('Name'), [InputRequired(), Length(max=80)])
    game = StringField(lazy_gettext('Game'), [Optional(), Length(max=80)])
    description = TextAreaField(
        lazy_gettext('Description'), [Optional(), Length(max=4000)]
    )
    image_url = StringField(
        lazy_gettext('Image URL'), [Optional(), Length(max=256)]
    )
    ruleset = TextAreaField(
        lazy_gettext('Ruleset'), [Optional(), Length(max=4000)]
    )
    start_time = DateTimeLocalField(
        lazy_gettext('Start time'), validators=[Optional()]
    )
    contestant_type = SelectField(
        lazy_gettext('Contestant type'), validators=[Optional()]
    )
    tournament_mode = SelectField(
        lazy_gettext('Tournament mode'), validators=[Optional()]
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

    def set_contestant_type_choices(self):
        self.contestant_type.choices = _get_contestant_type_choices()

    def set_tournament_mode_choices(self):
        self.tournament_mode.choices = _get_tournament_mode_choices()


class TournamentCreateForm(_BaseForm):
    pass


class TournamentUpdateForm(_BaseForm):
    pass


class TeamCreateForm(LocalizedForm):
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
    new_captain = SelectField(
        lazy_gettext('New captain'), [InputRequired()]
    )


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

        if not screen_name_validator.contains_only_valid_chars(
            screen_name
        ):
            raise ValidationError(
                lazy_gettext('Contains invalid characters.')
            )

        user = user_service.find_user_by_screen_name(screen_name)
        if user is None:
            raise ValidationError(lazy_gettext('Unknown username'))

        field.data = screen_name
        form.user = user
