"""
byceps.services.chair_optout.blueprints.site.forms
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

:License: Revised BSD (see `LICENSE` file for details)
"""

from flask_babel import lazy_gettext
from wtforms import RadioField
from wtforms.validators import InputRequired

from byceps.util.l10n import LocalizedForm


class ChairInformationForm(LocalizedForm):
    choice = RadioField(
        lazy_gettext('Chair information'),
        choices=[
            ('own', lazy_gettext('I will bring my own chair')),
            ('provided', lazy_gettext('I need a provided chair')),
        ],
        validators=[InputRequired()],
    )
