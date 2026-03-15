"""
byceps.services.lan_tournament.blueprints.site.forms
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

:Copyright: 2014-2026 Jochen Kupperschmidt
:License: Revised BSD (see `LICENSE` file for details)
"""

from flask_babel import lazy_gettext
from wtforms import IntegerField, StringField, TextAreaField
from wtforms.validators import InputRequired, Length, NumberRange, Optional

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
    description = TextAreaField(
        lazy_gettext('Description'), [Optional(), Length(max=2000)]
    )


class HighscoreSubmitForm(LocalizedForm):
    score = IntegerField(
        lazy_gettext('Score'),
        [InputRequired(), NumberRange(min=0, max=999999999)],
    )
    note = StringField(
        lazy_gettext('Note'), [Optional(), Length(max=200)]
    )
