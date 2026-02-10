from flask_babel import lazy_gettext
from wtforms import (
    DateTimeLocalField,
    IntegerField,
    SelectField,
    StringField,
    TextAreaField,
)
from wtforms.validators import InputRequired, Length, Optional

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
