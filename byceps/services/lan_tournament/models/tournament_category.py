from enum import Enum

from flask_babel import lazy_gettext


class TournamentCategory(Enum):
    MAIN = 'MAIN'
    FUN = 'FUN'
    STAGE = 'STAGE'
    USER_ORGANIZED = 'USER_ORGANIZED'

    @property
    def label(self) -> str:
        return _LABELS[self]


_LABELS = {
    TournamentCategory.MAIN: lazy_gettext('Main tournaments'),
    TournamentCategory.FUN: lazy_gettext('Fun tournaments'),
    TournamentCategory.STAGE: lazy_gettext('Stage / Offline'),
    TournamentCategory.USER_ORGANIZED: lazy_gettext('User Organized'),
}
