from flask_babel import lazy_gettext

from byceps.util.authz import register_permissions


register_permissions(
    'lan_tournament',
    [
        ('administrate', lazy_gettext('Administrate LAN tournaments')),
        ('create', lazy_gettext('Create LAN tournaments')),
        ('delete', lazy_gettext('Delete LAN tournaments')),
        ('update', lazy_gettext('Edit LAN tournaments')),
        ('view', lazy_gettext('View LAN tournaments')),
    ],
)
