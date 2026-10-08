"""
byceps.services.lan_tournament.blueprints.common.views
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from byceps.util.framework.blueprint import create_blueprint


# Template-only: no routes and no hooks, shared by the admin and site parents.
blueprint = create_blueprint('lan_tournament_common', __name__)
