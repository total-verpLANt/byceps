"""
byceps.services.chair_optout.blueprints.admin.forms
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

:License: Revised BSD (see `LICENSE` file for details)
"""

from flask_babel import lazy_gettext
from wtforms import SelectField
from wtforms.validators import InputRequired

from byceps.util.l10n import LocalizedForm


class RentalSelectionForm(LocalizedForm):
    enabled = SelectField(
        lazy_gettext('Rental chair selection'),
        choices=[('false', lazy_gettext('OFF')), ('true', lazy_gettext('ON'))],
        validators=[InputRequired()],
    )
