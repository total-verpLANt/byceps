"""
byceps.services.chair_optout.blueprints.site.forms
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

:Copyright: 2014-2026 Jochen Kupperschmidt
:License: Revised BSD (see `LICENSE` file for details)
"""

from flask_babel import lazy_gettext

from byceps.util.forms import MultiCheckboxField
from byceps.util.l10n import LocalizedForm


class ChairOptoutForm(LocalizedForm):
    ticket_ids = MultiCheckboxField(
        lazy_gettext('Eigenen Stuhl mitbringen'),
        choices=[],
    )
