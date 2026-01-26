"""
byceps.services.chair_optout.permissions
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

:Copyright: 2014-2026 Jochen Kupperschmidt
:License: Revised BSD (see `LICENSE` file for details)
"""

from flask_babel import lazy_gettext

from byceps.util.authz import register_permissions


register_permissions(
    'chair_optout',
    [
        ('view_report', lazy_gettext('View chair opt-out report')),
        ('export_report', lazy_gettext('Export chair opt-out report')),
    ],
)
