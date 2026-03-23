from flask_babel import lazy_gettext
from wtforms import (
    BooleanField,
    DateTimeLocalField,
    IntegerField,
    SelectField,
    StringField,
    TextAreaField,
)
from wtforms.validators import (
    InputRequired,
    Length,
    NumberRange,
    Optional,
    ValidationError,
)

from byceps.services.user import screen_name_validator, user_service
from byceps.util.l10n import LocalizedForm

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
        lazy_gettext('Start time'), validators=[Optional()]
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
        [Optional(), NumberRange(min=2)],
    )
    group_size_max = IntegerField(
        lazy_gettext('Max. group size'),
        [Optional(), NumberRange(min=2)],
    )
    advancement_count = IntegerField(
        lazy_gettext('Advance per group'),
        [Optional(), NumberRange(min=1)],
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
    pass


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


class HighscoreSubmitForm(LocalizedForm):
    contestant = SelectField(lazy_gettext('Contestant'))
    score = IntegerField(
        lazy_gettext('Score'),
        validators=[InputRequired(), NumberRange(min=0)],
    )
    note = StringField(lazy_gettext('Note'))
