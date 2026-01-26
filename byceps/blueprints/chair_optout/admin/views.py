"""
byceps.blueprints.chair_optout.admin.views
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

:Copyright: 2014-2026 Jochen Kupperschmidt
:License: Revised BSD (see `LICENSE` file for details)
"""

from byceps.util.framework.blueprint import create_blueprint
from byceps.util.framework.templating import templated


blueprint = create_blueprint('chair_optout_admin', __name__)


@blueprint.get('/for_party/<party_id>')
@templated
def index(party_id):
    """Show the chair opt-out report placeholder for a party."""
    return {'party_id': party_id}
