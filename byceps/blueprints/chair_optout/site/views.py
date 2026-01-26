"""
byceps.blueprints.chair_optout.site.views
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

:Copyright: 2014-2026 Jochen Kupperschmidt
:License: Revised BSD (see `LICENSE` file for details)
"""

from byceps.util.framework.blueprint import create_blueprint
from byceps.util.framework.templating import templated


blueprint = create_blueprint('chair_optout', __name__)


@blueprint.get('/')
@templated
def index():
    """Show the chair opt-out page."""
    return {}
