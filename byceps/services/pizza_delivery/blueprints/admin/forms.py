"""
byceps.services.pizza_delivery.blueprints.admin.forms
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

:Copyright: 2014-2026 Jochen Kupperschmidt
:License: Revised BSD (see `LICENSE` file for details)
"""

from flask_babel import lazy_gettext
from wtforms import StringField
from wtforms.validators import InputRequired, Length, Optional

from byceps.util.l10n import LocalizedForm


class CreateEntryForm(LocalizedForm):
    number = StringField(lazy_gettext('Number'), [InputRequired(), Length(max=20)])
    username = StringField(lazy_gettext('Username'), [Optional()])


class UpdateEntryForm(LocalizedForm):
    username = StringField(lazy_gettext('Username'), [Optional()])
