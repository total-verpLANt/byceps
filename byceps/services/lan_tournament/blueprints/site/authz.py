"""
byceps.services.lan_tournament.blueprints.site.authz
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Scoped authorization for the LAN tournament site blueprint.
"""

from functools import wraps
import uuid

from flask import abort, g

from byceps.services.authn.session.models import CurrentUser
from byceps.services.lan_tournament import (
    tournament_match_service,
    tournament_orga_service,
)
from byceps.services.lan_tournament.models.tournament import TournamentID
from byceps.services.lan_tournament.models.tournament_match import (
    TournamentMatchID,
)


def may_administrate_tournament(
    user: CurrentUser, tournament_id: TournamentID
) -> bool:
    """Return `True` if the user is a global tournament admin or an
    orga of that tournament.
    """
    if not user.authenticated:
        return False

    return user.has_permission(
        'lan_tournament.administrate'
    ) or tournament_orga_service.is_orga_for_tournament(user.id, tournament_id)


def scoped_orga_required(view_func):
    """Ensure the current user may administrate the tournament given
    by the view's `tournament_id` or `match_id`.
    """

    @wraps(view_func)
    def wrapper(*args, **kwargs):
        tournament_id = kwargs.get('tournament_id')

        if tournament_id is not None:
            try:
                uuid.UUID(str(tournament_id))
            except ValueError:
                abort(404)
        else:
            match_id = kwargs.get('match_id')
            try:
                match = tournament_match_service.get_match(
                    TournamentMatchID(uuid.UUID(str(match_id)))
                )
            except ValueError:
                abort(404)
            tournament_id = match.tournament_id

        if not may_administrate_tournament(g.user, tournament_id):
            abort(403)

        return view_func(*args, **kwargs)

    return wrapper
