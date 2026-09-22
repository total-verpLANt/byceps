import uuid
from flask import abort, g, request
from flask_babel import gettext

from byceps.services.lan_tournament import (
    tournament_match_service,
    tournament_orga_service,
    tournament_participant_service,
    tournament_score_service,
    tournament_service,
    tournament_team_service,
)
from byceps.services.lan_tournament.models.tournament import Tournament
from byceps.services.lan_tournament.models.tournament_match import (
    TournamentMatchID,
)
from byceps.services.lan_tournament.models.tournament_team import (
    TournamentTeam,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.lan_tournament.lan_tournament_view_helpers import (
    build_contestant_name_lookups,
    build_ffa_standings,
    build_hover_lookups,
    build_round_robin_standings,
    build_seat_lookup,
    compute_feed_counts,
    is_ffa_tournament,
    is_walkover_match,
    parse_match_ids,
    parse_submitted_contestant_scores,
    parse_submitted_ffa_placements,
    serialize_bracket_json,
)
from byceps.services.lan_tournament.tournament_match_service import (
    acknowledgement_match_ids,
)
from byceps.services.lan_tournament.models.contestant_type import (
    ContestantType,
)
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import (
    GameFormat,
)
from byceps.services.party import party_service
from byceps.services.user import user_service
from byceps.services.user.models import UserID
from byceps.util.framework.blueprint import create_blueprint
from byceps.util.framework.flash import flash_error, flash_success
from byceps.util.framework.templating import templated
from byceps.util.result import Err, Ok
from byceps.util.views import login_required, redirect_to

from .authz import may_administrate_tournament, scoped_orga_required
from .forms import (
    HighscoreSubmitForm,
    MatchCommentForm,
    OrgaMatchCorrectionForm,
    OrgaMatchUnconfirmForm,
    SiteTeamCreateForm,
    SiteTeamUpdateForm,
)


blueprint = create_blueprint('lan_tournament', __name__)


@blueprint.get('/')
@templated
def index():
    """List all tournaments for the current party."""
    party = _get_current_party_or_404()

    tournaments = tournament_service.get_tournaments_for_party(party.id)

    # Hide drafts from site visitors.
    visible_tournaments = [
        t
        for t in tournaments
        if t.tournament_status and t.tournament_status != TournamentStatus.DRAFT
    ]

    # Sort by admin-defined position (data arrives pre-sorted from
    # get_tournaments_for_party, but re-sort the visible subset).
    visible_tournaments.sort(key=lambda t: t.position)

    tournament_ids = [t.id for t in visible_tournaments]
    participant_counts = tournament_service.get_participant_counts_for_tournaments(tournament_ids)
    team_counts = tournament_team_service.get_team_counts_for_tournaments(tournament_ids)

    return {
        'tournaments': visible_tournaments,
        'participant_counts': participant_counts,
        'team_counts': team_counts,
    }


@blueprint.get('/<tournament_id>')
@templated
def view(tournament_id):
    """Show the tournament."""
    tournament = _get_tournament_or_404(tournament_id)

    # Hide drafts from site visitors.
    if (
        tournament.tournament_status
        and tournament.tournament_status == TournamentStatus.DRAFT
    ):
        abort(404)

    participants = (
        tournament_participant_service.get_participants_for_tournament(
            tournament.id
        )
    )
    participant_count = len(participants)

    current_user_participant = None
    if g.user.authenticated:
        for p in participants:
            if p.user_id == g.user.id:
                current_user_participant = p
                break

    can_join = (
        g.user.authenticated
        and current_user_participant is None
        and tournament.tournament_status == TournamentStatus.REGISTRATION_OPEN
        and (
            tournament.max_players is None
            or participant_count < tournament.max_players
        )
    )

    can_leave = (
        g.user.authenticated
        and current_user_participant is not None
        and tournament.tournament_status == TournamentStatus.REGISTRATION_OPEN
    )

    # Team data for team tournaments
    is_team_tournament = (
        tournament.contestant_type == ContestantType.TEAM
    )
    teams = []
    team_count = 0
    member_counts = {}
    if is_team_tournament:
        teams = tournament_team_service.get_teams_for_tournament(
            tournament.id
        )
        team_count = len(teams)
        member_counts = tournament_team_service.get_team_member_counts(
            tournament.id
        )

    # Resolve user names
    user_ids = {p.user_id for p in participants}
    users_by_id = user_service.get_users_indexed_by_id(user_ids)

    # Build seat lookup
    seats_by_user_id = build_seat_lookup(user_ids, tournament.party_id)

    winner_name = tournament_service.resolve_winner_display_name(tournament)
    podium = tournament_service.resolve_podium_display_names(tournament)
    runner_up_name = podium.get('runner_up')
    bronze_name = podium.get('bronze')

    may_administrate = may_administrate_tournament(g.user, tournament.id)

    orgas = tournament_orga_service.get_public_orgas_for_tournament(
        tournament.id
    )

    return {
        'tournament': tournament,
        'participants': participants,
        'participant_count': participant_count,
        'is_team_tournament': is_team_tournament,
        'teams': teams,
        'team_count': team_count,
        'member_counts': member_counts,
        'current_user_participant': current_user_participant,
        'can_join': can_join,
        'can_leave': can_leave,
        'users_by_id': users_by_id,
        'seats_by_user_id': seats_by_user_id,
        'winner_name': winner_name,
        'runner_up_name': runner_up_name,
        'bronze_name': bronze_name,
        'may_administrate': may_administrate,
        'orgas': orgas,
        'active_tab': 'overview',
    }


@blueprint.post('/<tournament_id>/join')
@login_required
def join(tournament_id):
    """Join the tournament."""
    tournament = _get_tournament_or_404(tournament_id)

    if tournament.tournament_status != TournamentStatus.REGISTRATION_OPEN:
        flash_error(gettext('Registration is not open.'))
        return redirect_to('.view', tournament_id=tournament.id)

    match tournament_participant_service.join_tournament(
        tournament.id, g.user.id
    ):
        case Ok((_participant, _event)):
            flash_success(
                gettext(
                    'You have joined the tournament "%(name)s".',
                    name=tournament.name,
                )
            )
        case Err(error_message):
            flash_error(
                gettext(
                    'Could not join: %(error)s',
                    error=error_message,
                )
            )

    return redirect_to('.view', tournament_id=tournament.id)


@blueprint.post('/<tournament_id>/leave')
@login_required
def leave(tournament_id):
    """Leave the tournament."""
    tournament = _get_tournament_or_404(tournament_id)

    # Find the current user's participation.
    participants = (
        tournament_participant_service.get_participants_for_tournament(
            tournament.id
        )
    )
    current_user_participant = None
    for p in participants:
        if p.user_id == g.user.id:
            current_user_participant = p
            break

    if current_user_participant is None:
        flash_error(gettext('You are not participating in this tournament.'))
        return redirect_to('.view', tournament_id=tournament.id)

    match tournament_participant_service.leave_tournament(
        tournament.id, current_user_participant.id
    ):
        case Ok(_event):
            flash_success(
                gettext(
                    'You have left the tournament "%(name)s".',
                    name=tournament.name,
                )
            )
        case Err(error_message):
            flash_error(
                gettext(
                    'Could not leave: %(error)s',
                    error=error_message,
                )
            )

    return redirect_to('.view', tournament_id=tournament.id)


@blueprint.get('/<tournament_id>/teams')
@templated
def teams(tournament_id):
    """List all teams for the tournament."""
    tournament = _get_tournament_or_404(tournament_id)

    # Hide drafts from site visitors.
    if (
        tournament.tournament_status
        and tournament.tournament_status == TournamentStatus.DRAFT
    ):
        abort(404)

    _require_team_tournament(tournament)

    teams = tournament_team_service.get_teams_for_tournament(tournament.id)
    member_counts = tournament_team_service.get_team_member_counts(
        tournament.id
    )

    return {
        'tournament': tournament,
        'teams': teams,
        'member_counts': member_counts,
        'active_tab': 'teams',
    }


@blueprint.get('/<tournament_id>/teams/create')
@login_required
@templated
def create_team_form(tournament_id, erroneous_form=None):
    """Show form to create a team."""
    tournament = _get_tournament_or_404(tournament_id)

    _require_team_tournament(tournament)

    if tournament.tournament_status != TournamentStatus.REGISTRATION_OPEN:
        flash_error(gettext('Registration is not open.'))
        return redirect_to('.view', tournament_id=tournament.id)

    form = erroneous_form if erroneous_form else SiteTeamCreateForm()

    return {
        'tournament': tournament,
        'form': form,
    }


@blueprint.post('/<tournament_id>/teams/create')
@login_required
def create_team(tournament_id):
    """Create a team."""
    tournament = _get_tournament_or_404(tournament_id)

    _require_team_tournament(tournament)

    if tournament.tournament_status != TournamentStatus.REGISTRATION_OPEN:
        flash_error(gettext('Registration is not open.'))
        return redirect_to('.view', tournament_id=tournament.id)

    form = SiteTeamCreateForm(request.form)
    if not form.validate():
        return create_team_form(tournament_id, form)

    name = form.name.data.strip()
    tag = form.tag.data.strip() if form.tag.data else None
    description = form.description.data.strip() if form.description.data else None
    join_code = form.join_code.data.strip() if form.join_code.data else None

    match tournament_team_service.create_team(
        tournament.id,
        name,
        g.user.id,
        tag=tag,
        description=description,
        join_code=join_code,
    ):
        case Ok((team, _event)):
            flash_success(
                gettext(
                    'Team "%(name)s" has been created.',
                    name=team.name,
                )
            )
            return redirect_to('.view_team', team_id=team.id)
        case Err(error_message):
            flash_error(
                gettext(
                    'Could not create team: %(error)s',
                    error=error_message,
                )
            )
            return redirect_to('.create_team_form', tournament_id=tournament.id)


@blueprint.get('/teams/<team_id>')
@templated
def view_team(team_id):
    """Show team details."""
    team = _get_team_or_404(team_id)
    tournament = _get_tournament_or_404(team.tournament_id)

    # Hide drafts from site visitors.
    if (
        tournament.tournament_status
        and tournament.tournament_status == TournamentStatus.DRAFT
    ):
        abort(404)

    _require_team_tournament(tournament)

    # Get all participants for this team.
    all_participants = (
        tournament_participant_service.get_participants_for_tournament(
            tournament.id
        )
    )
    team_members = [p for p in all_participants if p.team_id == team.id]

    current_user_participant = None
    if g.user.authenticated:
        for p in all_participants:
            if p.user_id == g.user.id:
                current_user_participant = p
                break

    is_team_member = (
        current_user_participant is not None
        and current_user_participant.team_id == team.id
    )

    can_join_team = (
        g.user.authenticated
        and current_user_participant is not None
        and current_user_participant.team_id is None
        and tournament.tournament_status == TournamentStatus.REGISTRATION_OPEN
    )

    can_leave_team = (
        g.user.authenticated
        and is_team_member
        and tournament.tournament_status == TournamentStatus.REGISTRATION_OPEN
    )

    # Resolve user names for team members
    user_ids = {m.user_id for m in team_members}
    user_ids.add(team.captain_user_id)
    users_by_id = user_service.get_users_indexed_by_id(user_ids)

    # Build seat lookup
    seats_by_user_id = build_seat_lookup(user_ids, tournament.party_id)

    # Captain management context for the template.
    is_captain = (
        g.user.authenticated
        and g.user.id == team.captain_user_id
    )
    can_manage_team = (
        is_captain
        and _is_captain_management_allowed(tournament)
    )

    return {
        'tournament': tournament,
        'team': team,
        'team_members': team_members,
        'current_user_participant': current_user_participant,
        'is_team_member': is_team_member,
        'is_captain': is_captain,
        'can_manage_team': can_manage_team,
        'can_join_team': can_join_team,
        'can_leave_team': can_leave_team,
        'users_by_id': users_by_id,
        'seats_by_user_id': seats_by_user_id,
        'active_tab': 'teams',
    }


@blueprint.post('/teams/<team_id>/join')
@login_required
def join_team(team_id):
    """Join a team."""
    team = _get_team_or_404(team_id)
    tournament = _get_tournament_or_404(team.tournament_id)

    _require_team_tournament(tournament)

    if tournament.tournament_status != TournamentStatus.REGISTRATION_OPEN:
        flash_error(gettext('Registration is not open.'))
        return redirect_to('.view_team', team_id=team_id)

    # Find current user's participant record.
    participants = (
        tournament_participant_service.get_participants_for_tournament(
            tournament.id
        )
    )
    current_user_participant = None
    for p in participants:
        if p.user_id == g.user.id:
            current_user_participant = p
            break

    if current_user_participant is None:
        flash_error(gettext('You must join the tournament first.'))
        return redirect_to('.view', tournament_id=tournament.id)

    if current_user_participant.team_id is not None:
        flash_error(gettext('You are already in a team.'))
        return redirect_to('.view_team', team_id=team_id)

    # Get join code from form if team requires one.
    join_code = request.form.get('join_code', '').strip() or None

    match tournament_team_service.join_team(
        current_user_participant.id, team_id, join_code=join_code
    ):
        case Ok(_event):
            flash_success(
                gettext(
                    'You have joined team "%(name)s".',
                    name=team.name,
                )
            )
        case Err(error_message):
            flash_error(
                gettext(
                    'Could not join team: %(error)s',
                    error=error_message,
                )
            )

    return redirect_to('.view_team', team_id=team_id)


@blueprint.post('/teams/<team_id>/leave')
@login_required
def leave_team(team_id):
    """Leave a team."""
    team = _get_team_or_404(team_id)
    tournament = _get_tournament_or_404(team.tournament_id)

    _require_team_tournament(tournament)

    if tournament.tournament_status != TournamentStatus.REGISTRATION_OPEN:
        flash_error(gettext('Registration is not open.'))
        return redirect_to('.view_team', team_id=team_id)

    # Find current user's participant record.
    participants = (
        tournament_participant_service.get_participants_for_tournament(
            tournament.id
        )
    )
    current_user_participant = None
    for p in participants:
        if p.user_id == g.user.id:
            current_user_participant = p
            break

    if current_user_participant is None:
        flash_error(gettext('You are not participating in this tournament.'))
        return redirect_to('.view', tournament_id=tournament.id)

    if current_user_participant.team_id != team.id:
        flash_error(gettext('You are not in this team.'))
        return redirect_to('.view_team', team_id=team_id)

    match tournament_team_service.leave_team(current_user_participant.id):
        case Ok(_event):
            flash_success(
                gettext(
                    'You have left team "%(name)s".',
                    name=team.name,
                )
            )
        case Err(error_message):
            flash_error(
                gettext(
                    'Could not leave team: %(error)s',
                    error=error_message,
                )
            )

    return redirect_to('.view_team', team_id=team_id)


# -------------------------------------------------------------------- #
# captain management helpers
# -------------------------------------------------------------------- #


def _require_team_tournament(tournament) -> None:
    """Abort with 404 if the tournament does not use teams."""
    if (
        tournament.contestant_type is None
        or tournament.contestant_type != ContestantType.TEAM
    ):
        abort(404)


def _is_captain_management_allowed(tournament) -> bool:
    """Return True if the tournament status allows captain management.

    Captain management (update team, transfer captain, remove member) is
    permitted during REGISTRATION_OPEN and ONGOING, but blocked during
    DRAFT, COMPLETED, and other states.
    """
    return tournament.tournament_status in (
        TournamentStatus.REGISTRATION_OPEN,
        TournamentStatus.ONGOING,
    )


def _require_team_captain(tournament, team):
    """Abort with 403 if the current user is not the team captain.

    Also aborts if captain management is not allowed for the current
    tournament status (flashes an error in that case).
    """
    if not g.user.authenticated:
        abort(403)

    if g.user.id != team.captain_user_id:
        abort(403)

    if not _is_captain_management_allowed(tournament):
        flash_error(gettext('Team management is not available at this time.'))
        abort(403)


def _get_team_member_user_ids(
    team_id,
) -> set:
    """Return active member user IDs for view-layer IDOR validation."""
    members = tournament_team_service.get_team_members(team_id)
    return {m.user_id for m in members}


# -------------------------------------------------------------------- #
# captain management routes
# -------------------------------------------------------------------- #


@blueprint.get('/<tournament_id>/teams/<team_id>/update')
@login_required
@templated
def update_team_form(tournament_id, team_id, erroneous_form=None):
    """Show form to update team name/description (captain only)."""
    tournament = _get_tournament_or_404(tournament_id)
    team = _get_team_or_404(team_id)

    _require_team_tournament(tournament)

    if team.tournament_id != tournament.id:
        abort(404)

    _require_team_captain(tournament, team)

    form = erroneous_form if erroneous_form else SiteTeamUpdateForm(obj=team)

    return {
        'tournament': tournament,
        'team': team,
        'form': form,
    }


@blueprint.post('/<tournament_id>/teams/<team_id>/update')
@login_required
def update_team(tournament_id, team_id):
    """Process team update form (captain only)."""
    tournament = _get_tournament_or_404(tournament_id)
    team = _get_team_or_404(team_id)

    _require_team_tournament(tournament)

    if team.tournament_id != tournament.id:
        abort(404)

    _require_team_captain(tournament, team)

    form = SiteTeamUpdateForm(request.form)
    if not form.validate():
        return update_team_form(tournament_id, team_id, form)

    name = form.name.data.strip()
    tag = form.tag.data.strip() if form.tag.data else None
    description = form.description.data.strip() if form.description.data else None
    join_code = form.join_code.data.strip() if form.join_code.data else None

    match tournament_team_service.update_team(
        team.id,
        name=name,
        tag=tag,
        description=description,
        image_url=team.image_url,
        join_code=join_code,
        current_user_id=g.user.id,
    ):
        case Ok(updated_team):
            flash_success(
                gettext(
                    'Team "%(name)s" has been updated.',
                    name=updated_team.name,
                )
            )
            return redirect_to('.view_team', team_id=team.id)
        case Err(error_message):
            flash_error(
                gettext(
                    'Could not update team: %(error)s',
                    error=error_message,
                )
            )
            return redirect_to(
                '.update_team_form',
                tournament_id=tournament.id,
                team_id=team.id,
            )


@blueprint.post('/<tournament_id>/teams/<team_id>/transfer_captain')
@login_required
def site_transfer_captain(tournament_id, team_id):
    """Transfer captain role to another team member (captain only)."""
    tournament = _get_tournament_or_404(tournament_id)
    team = _get_team_or_404(team_id)

    _require_team_tournament(tournament)

    if team.tournament_id != tournament.id:
        abort(404)

    _require_team_captain(tournament, team)

    new_captain_user_id = request.form.get('new_captain_id', '').strip()

    if not new_captain_user_id:
        flash_error(gettext('No user selected.'))
        return redirect_to('.view_team', team_id=team.id)

    try:
        new_captain_user_id = UserID(uuid.UUID(new_captain_user_id))
    except ValueError:
        flash_error(gettext('Invalid user selected.'))
        return redirect_to('.view_team', team_id=team.id)

    # View-layer IDOR guard: verify the target is a real team member.
    member_user_ids = _get_team_member_user_ids(team.id)
    if new_captain_user_id not in member_user_ids:
        flash_error(gettext('Selected user is not a team member.'))
        return redirect_to('.view_team', team_id=team.id)
    if new_captain_user_id == team.captain_user_id:
        flash_error(gettext('User is already the captain.'))
        return redirect_to('.view_team', team_id=team.id)

    match tournament_team_service.transfer_captain(
        team.id, new_captain_user_id
    ):
        case Ok(_updated_team):
            flash_success(
                gettext('Captain role has been transferred.')
            )
        case Err(error_message):
            flash_error(
                gettext(
                    'Could not transfer captain: %(error)s',
                    error=error_message,
                )
            )

    return redirect_to('.view_team', team_id=team.id)


@blueprint.post('/<tournament_id>/teams/<team_id>/remove_member')
@login_required
def site_remove_member(tournament_id, team_id):
    """Remove a non-captain member from the team (captain only)."""
    tournament = _get_tournament_or_404(tournament_id)
    team = _get_team_or_404(team_id)

    _require_team_tournament(tournament)

    if team.tournament_id != tournament.id:
        abort(404)

    _require_team_captain(tournament, team)

    user_id = request.form.get('user_id', '').strip()
    if not user_id:
        flash_error(gettext('No user selected.'))
        return redirect_to('.view_team', team_id=team.id)

    try:
        user_id = UserID(uuid.UUID(user_id))
    except ValueError:
        flash_error(gettext('Invalid user selected.'))
        return redirect_to('.view_team', team_id=team.id)

    # View-layer IDOR guard: verify the target is a real team member.
    member_user_ids = _get_team_member_user_ids(team.id)
    if user_id not in member_user_ids:
        flash_error(gettext('Selected user is not a team member.'))
        return redirect_to('.view_team', team_id=team.id)
    if user_id == team.captain_user_id:
        flash_error(gettext('Cannot remove the team captain.'))
        return redirect_to('.view_team', team_id=team.id)

    match tournament_team_service.remove_team_member(team.id, user_id):
        case Ok(_event):
            flash_success(
                gettext('Member has been removed from the team.')
            )
        case Err(error_message):
            flash_error(
                gettext(
                    'Could not remove member: %(error)s',
                    error=error_message,
                )
            )

    return redirect_to('.view_team', team_id=team.id)


def _get_current_party_or_404():
    if not g.party:
        abort(404)

    party = party_service.find_party(g.party.id)
    if party is None:
        abort(404)

    return party


def _get_tournament_or_404(tournament_id) -> Tournament:
    try:
        uuid.UUID(str(tournament_id))
    except ValueError:
        abort(404)

    tournament = tournament_service.find_tournament(tournament_id)

    if tournament is None:
        abort(404)

    # Cross-party isolation: tournament must belong to the current site's party.
    # Fail-closed — if g.party is missing the site is misconfigured; deny access.
    if not g.party or not g.party.id or tournament.party_id != g.party.id:
        abort(404)

    return tournament


def _get_team_or_404(team_id) -> TournamentTeam:
    try:
        uuid.UUID(str(team_id))
    except ValueError:
        abort(404)

    team = tournament_team_service.find_team(team_id)

    if team is None:
        abort(404)

    if team.removed_at is not None:
        abort(404)

    return team


# -------------------------------------------------------------------- #
# matches


def _is_match_ready(entry: dict) -> bool:
    """A match is ready when it has 2+ contestants and is NOT confirmed."""
    return (
        len(entry['contestants']) >= 2
        and entry['match'].confirmed_by is None
    )


def _is_match_open(entry: dict) -> bool:
    """A match is open when it has 1+ contestant and is NOT confirmed.

    This is a superset of ready — every ready match is also open.
    """
    return (
        len(entry['contestants']) >= 1
        and entry['match'].confirmed_by is None
    )


def _is_user_match(entry: dict, participant) -> bool:
    """Check if any contestant in the match belongs to this participant."""
    for c in entry['contestants']:
        if c.team_id and participant.team_id and c.team_id == participant.team_id:
            return True
        if c.participant_id and c.participant_id == participant.id:
            return True
    return False


@blueprint.get('/<tournament_id>/matches')
@templated
def matches(tournament_id):
    """List all matches for the tournament."""
    tournament = _get_tournament_or_404(tournament_id)

    # Hide drafts from site visitors.
    if (
        tournament.tournament_status
        and tournament.tournament_status == TournamentStatus.DRAFT
    ):
        abort(404)

    matches = tournament_match_service.get_matches_for_tournament_ordered(
        tournament.id
    )

    # Bulk-fetch all contestants for the tournament in one query (not N).
    contestants_by_match = (
        tournament_match_service.get_contestants_for_tournament(tournament.id)
    )

    match_data = []
    all_contestants = []
    for match in matches:
        contestants = contestants_by_match.get(match.id, [])
        match_data.append(
            {
                'match': match,
                'contestants': contestants,
            }
        )
        all_contestants.append(contestants)

    # Fetch participants once, share across both helpers.
    participants = (
        tournament_participant_service.get_participants_for_tournament(
            tournament.id
        )
    )

    # Determine current participant early — needed for personal-scope filtering.
    current_user_participant = None
    if g.user.authenticated:
        for p in participants:
            if p.user_id == g.user.id:
                current_user_participant = p
                break

    # Apply status filter based on ?only= param (all users).
    only = request.args.get('only', 'ready')

    if current_user_participant:
        # Participant: ready count is personal-scoped (my matches only).
        ready_count = sum(
            1 for e in match_data
            if _is_match_ready(e) and _is_user_match(e, current_user_participant)
        )
    else:
        # Anonymous / non-participant: ready count is tournament-wide.
        ready_count = sum(1 for e in match_data if _is_match_ready(e))

    open_count = sum(1 for e in match_data if _is_match_open(e))
    total_count = len(match_data)
    match_quantities = {
        'ready': ready_count,
        'open': open_count,
        'all': total_count,
    }

    if only == 'ready':
        if current_user_participant:
            match_data = [
                e for e in match_data
                if _is_match_ready(e) and _is_user_match(e, current_user_participant)
            ]
        else:
            match_data = [e for e in match_data if _is_match_ready(e)]
    elif only == 'open':
        match_data = [e for e in match_data if _is_match_open(e)]
    # 'all' → no filtering

    teams_by_id, participants_by_id = build_contestant_name_lookups(
        tournament.id, all_contestants, participants=participants
    )

    seats_by_user_id, team_members_by_team_id = build_hover_lookups(
        tournament, participants_by_id, teams_by_id, tournament.party_id,
        participants=participants,
    )

    return {
        'tournament': tournament,
        'match_data': match_data,
        'only': only,
        'match_quantities': match_quantities,
        'teams_by_id': teams_by_id,
        'participants_by_id': participants_by_id,
        'seats_by_user_id': seats_by_user_id,
        'team_members_by_team_id': team_members_by_team_id,
        'current_user_participant': current_user_participant,
        'active_tab': 'matches',
    }


@blueprint.get('/matches/<match_id>')
@templated
def view_match(match_id):
    """Show match details."""
    from byceps.services.lan_tournament.models.tournament_match import (
        TournamentMatchID,
    )

    try:
        match_id_obj = TournamentMatchID(uuid.UUID(match_id))
        match = tournament_match_service.get_match(match_id_obj)
    except ValueError:
        abort(404)
    tournament = _get_tournament_or_404(match.tournament_id)

    # Hide drafts from site visitors.
    if (
        tournament.tournament_status
        and tournament.tournament_status == TournamentStatus.DRAFT
    ):
        abort(404)

    contestants = tournament_match_service.get_contestants_for_match(
        match_id_obj
    )
    comments = tournament_match_service.get_comments_from_match(match_id_obj)

    teams_by_id, participants_by_id = build_contestant_name_lookups(
        tournament.id, [contestants]
    )

    seats_by_user_id, team_members_by_team_id = build_hover_lookups(
        tournament, participants_by_id, teams_by_id, tournament.party_id
    )

    # Resolve comment author names
    comment_user_ids = {c.created_by for c in comments}
    comment_users_by_id = user_service.get_users_indexed_by_id(comment_user_ids)

    # Participant / loser detection for score submission & confirmation.
    if g.user.authenticated:
        role = tournament_match_service.get_user_match_role(
            match.tournament_id, g.user.id, contestants,
            match_confirmed=match.confirmed_by is not None,
        )
    else:
        role = tournament_match_service.MatchUserRole(contestant=None, is_loser=False, can_confirm=False, can_submit=False)
    current_user_contestant = role.contestant
    current_user_is_loser = role.is_loser
    current_user_can_confirm = role.can_confirm
    current_user_can_submit = role.can_submit

    # Comment auth: match contestants OR tournament admins, during ONGOING.
    # get_user_match_role() returns contestant=None for confirmed matches,
    # so resolve separately with match_confirmed=False.
    if g.user.authenticated and tournament.tournament_status == TournamentStatus.ONGOING:
        comment_role = tournament_match_service.get_user_match_role(
            match.tournament_id, g.user.id, contestants,
            match_confirmed=False,
        )
        is_contestant = comment_role.contestant is not None
        is_admin = g.user.has_permission('lan_tournament.administrate')
        current_user_can_comment = is_contestant or is_admin
    else:
        current_user_can_comment = False
    comment_form = MatchCommentForm() if current_user_can_comment else None

    may_administrate = may_administrate_tournament(g.user, tournament.id)
    results_editable = _orga_results_editable(tournament)
    is_ffa = is_ffa_tournament(tournament)
    is_walkover = is_walkover_match(contestants)
    ffa_result_consumed = (
        is_ffa
        and may_administrate
        and results_editable
        and match.confirmed_by is not None
        and tournament_match_service.ffa_round_already_advanced(
            match, tournament
        )
    )

    # Show the orga what a correction would affect.
    correction_case = None
    affected_downstream_matches = []
    if (
        may_administrate
        and results_editable
        and not is_ffa
        and not is_walkover
        and match.confirmed_by is not None
    ):
        classification_result = (
            tournament_match_service.classify_result_correction(match.id)
        )
        if classification_result.is_ok():
            correction_case, affected_downstream_ids = (
                classification_result.unwrap()
            )
            # Batched fetch, then restore the service's order.
            fetched_by_id = {
                m.id: m
                for m in tournament_match_service.get_matches_by_ids(
                    affected_downstream_ids
                )
            }
            affected_downstream_matches = [
                fetched_by_id[downstream_id]
                for downstream_id in affected_downstream_ids
                if downstream_id in fetched_by_id
            ]

    ack_match_ids = (
        [
            str(ack_id)
            for ack_id in acknowledgement_match_ids(
                correction_case, affected_downstream_matches
            )
        ]
        if correction_case is not None
        else []
    )

    return {
        'tournament': tournament,
        'match': match,
        'contestants': contestants,
        'comments': comments,
        'teams_by_id': teams_by_id,
        'participants_by_id': participants_by_id,
        'seats_by_user_id': seats_by_user_id,
        'team_members_by_team_id': team_members_by_team_id,
        'comment_users_by_id': comment_users_by_id,
        'current_user_contestant': current_user_contestant,
        'current_user_is_loser': current_user_is_loser,
        'current_user_can_confirm': current_user_can_confirm,
        'current_user_can_submit': current_user_can_submit,
        'current_user_can_comment': current_user_can_comment,
        'comment_form': comment_form,
        'may_administrate': may_administrate,
        'results_editable': results_editable,
        'is_ffa': is_ffa,
        'is_walkover': is_walkover,
        'ffa_result_consumed': ffa_result_consumed,
        'correction_case': correction_case,
        'affected_downstream_matches': affected_downstream_matches,
        'ack_match_ids': ack_match_ids,
        'max_match_score': tournament_match_service.MAX_MATCH_SCORE,
        'active_tab': 'matches',
    }


@blueprint.post('/matches/<match_id>/set_score')
@login_required
def set_score(match_id):
    """Set scores for a match (proposed loser only)."""
    from byceps.services.lan_tournament.models.tournament_match import (
        TournamentMatchID,
    )

    try:
        match_id_obj = TournamentMatchID(uuid.UUID(match_id))
        match = tournament_match_service.get_match(match_id_obj)
    except ValueError:
        abort(404)

    # Enforce status guard — same pattern as join/create_team/join_team.
    tournament = _get_tournament_or_404(match.tournament_id)
    if tournament.tournament_status != TournamentStatus.ONGOING:
        flash_error(gettext('Tournament is not in progress.'))
        return redirect_to('.view_match', match_id=match_id)

    contestant_ids = request.form.getlist('contestant_id')
    scores_raw = request.form.getlist('score')
    if not contestant_ids or len(contestant_ids) != len(scores_raw):
        flash_error(gettext('Invalid form data.'))
        return redirect_to('.view_match', match_id=match_id)

    try:
        scores = {
            uuid.UUID(cid): int(s)
            for cid, s in zip(contestant_ids, scores_raw)
        }
    except (ValueError, AttributeError):
        flash_error(gettext('Invalid score or contestant ID.'))
        return redirect_to('.view_match', match_id=match_id)

    # Defense-in-depth: reject obviously invalid scores before hitting the service.
    for s in scores.values():
        if s < 0 or s > 999_999_999:
            flash_error(gettext('Score must be between 0 and 999,999,999.'))
            return redirect_to('.view_match', match_id=match_id)

    result = tournament_match_service.set_match_scores(
        match_id_obj, g.user.id, scores
    )
    if result.is_err():
        # CAUTION: `match` and `tournament` are expired/detached after
        # set_match_scores rolls back the session on Err.  Only use
        # `match_id` (the URL string) for the redirect — do NOT access
        # attributes on the ORM objects fetched above.
        flash_error(gettext(result.unwrap_err()))
    else:
        flash_success(gettext('Match result submitted.'))
    return redirect_to('.view_match', match_id=match_id)


@blueprint.post('/matches/<match_id>/add_comment')
@login_required
def add_comment(match_id):
    """Add a comment to a match (contestants or tournament admins)."""
    from byceps.services.lan_tournament.models.tournament_match import (
        TournamentMatchID,
    )

    try:
        match_id_obj = TournamentMatchID(uuid.UUID(match_id))
        match = tournament_match_service.get_match(match_id_obj)
    except ValueError:
        abort(404)

    tournament = _get_tournament_or_404(match.tournament_id)
    if tournament.tournament_status != TournamentStatus.ONGOING:
        flash_error(gettext('Tournament is not in progress.'))
        return redirect_to('.view_match', match_id=match_id)

    # Authorization: match contestants OR tournament admins.
    is_admin = g.user.has_permission('lan_tournament.administrate')
    contestants = tournament_match_service.get_contestants_for_match(
        match_id_obj
    )
    role = tournament_match_service.get_user_match_role(
        match.tournament_id, g.user.id, contestants,
        match_confirmed=False,
    )
    if role.contestant is None and not is_admin:
        abort(403)

    form = MatchCommentForm(request.form)
    if not form.validate():
        flash_error(gettext('Invalid comment.'))
        return redirect_to('.view_match', match_id=match_id)

    result = tournament_match_service.add_comment(
        match_id_obj, g.user.id, form.comment.data.strip()
    )
    if result.is_err():
        flash_error(gettext(result.unwrap_err()))
    else:
        flash_success(gettext('Comment added.'))
    return redirect_to('.view_match', match_id=match_id)


# -------------------------------------------------------------------- #
# orga actions


def _get_orga_match_and_tournament_or_404(match_id):
    """Return the match and its tournament, which must belong to the
    current site's party.
    """
    try:
        match_id_obj = TournamentMatchID(uuid.UUID(str(match_id)))
        match = tournament_match_service.get_match(match_id_obj)
    except ValueError:
        abort(404)

    tournament = _get_tournament_or_404(match.tournament_id)

    return match, tournament


def _orga_results_editable(tournament: Tournament) -> bool:
    """Return `True` if an orga may change match results now."""
    return tournament.tournament_status == TournamentStatus.ONGOING


@blueprint.post('/orga/matches/<match_id>/confirm_with_scores')
@login_required
@scoped_orga_required
def orga_confirm_match_with_scores(match_id):
    """Set scores for all contestants and confirm the match."""
    match, tournament = _get_orga_match_and_tournament_or_404(match_id)

    if not _orga_results_editable(tournament):
        flash_error(gettext('Tournament is not in progress.'))
        return redirect_to('.view_match', match_id=match.id)

    contestants = tournament_match_service.get_contestants_for_match(match.id)

    parse_result = parse_submitted_contestant_scores(
        contestants,
        tournament,
        request.form,
        field_prefix='score_',
        allow_all_blank=False,
    )
    if parse_result.is_err():
        flash_error(parse_result.unwrap_err())
        return redirect_to('.view_match', match_id=match_id)

    scores = parse_result.unwrap()

    match tournament_match_service.admin_set_and_confirm_match(
        match.id, g.user.id, scores
    ):
        case Ok(_):
            flash_success(gettext('Match has been confirmed.'))
        case Err(error_message):
            flash_error(
                gettext(
                    'Error confirming match: %(error)s',
                    error=gettext(error_message),
                )
            )

    return redirect_to('.view_match', match_id=match_id)


@blueprint.post('/orga/matches/<match_id>/unconfirm')
@login_required
@scoped_orga_required
def orga_unconfirm_match(match_id):
    """Unconfirm a match result."""
    match, tournament = _get_orga_match_and_tournament_or_404(match_id)

    if not _orga_results_editable(tournament):
        flash_error(gettext('Tournament is not in progress.'))
        return redirect_to('.view_match', match_id=match.id)

    if not is_ffa_tournament(tournament):
        flash_error(
            gettext(
                'Bracket matches are retracted in the result '
                'correction panel, which requires acknowledging the '
                'impact on downstream matches.'
            )
        )
        return redirect_to('.view_match', match_id=match.id)

    form = OrgaMatchUnconfirmForm(request.form)
    if not form.validate():
        flash_error(gettext('Reason cannot be empty.'))
        return redirect_to('.view_match', match_id=match_id)

    reason = form.reason.data.strip()
    if not reason:
        flash_error(gettext('Reason cannot be empty.'))
        return redirect_to('.view_match', match_id=match_id)

    match tournament_match_service.unconfirm_match(
        match.id, g.user.id, reason=reason
    ):
        case Ok(_):
            flash_success(gettext('Match has been unconfirmed.'))
        case Err(error_message):
            flash_error(
                gettext(
                    'Error unconfirming match: %(error)s',
                    error=gettext(error_message),
                )
            )

    return redirect_to('.view_match', match_id=match_id)


@blueprint.post('/orga/matches/<match_id>/correct_result')
@login_required
@scoped_orga_required
def orga_correct_match_result(match_id):
    """Correct a match result: retract it and optionally re-enter scores."""
    match, tournament = _get_orga_match_and_tournament_or_404(match_id)

    if not _orga_results_editable(tournament):
        flash_error(gettext('Tournament is not in progress.'))
        return redirect_to('.view_match', match_id=match.id)

    if is_ffa_tournament(tournament):
        flash_error(
            gettext(
                'Free-for-all matches are corrected by unconfirming '
                'them and re-entering the placements.'
            )
        )
        return redirect_to('.view_match', match_id=match.id)

    form = OrgaMatchCorrectionForm(request.form)
    if not form.validate():
        flash_error(gettext('Invalid input.'))
        return redirect_to('.view_match', match_id=match_id)

    reason = form.reason.data.strip()
    ack_critical = bool(form.ack_critical.data)
    acknowledged_match_ids = parse_match_ids(
        request.form.get('ack_match_ids', '')
    )

    contestants = tournament_match_service.get_contestants_for_match(match.id)

    parse_result = parse_submitted_contestant_scores(
        contestants,
        tournament,
        request.form,
        field_prefix='corrected_score_',
        allow_all_blank=True,
    )
    if parse_result.is_err():
        flash_error(parse_result.unwrap_err())
        return redirect_to('.view_match', match_id=match_id)

    corrected_scores = parse_result.unwrap() or None

    result = tournament_match_service.correct_match_result(
        match.id,
        g.user.id,
        reason=reason,
        corrected_scores=corrected_scores,
        ack_critical=ack_critical,
        acknowledged_match_ids=acknowledged_match_ids,
    )

    match result:
        case Ok((_, True)):
            flash_success(
                gettext(
                    'Match result has been corrected and the new scores confirmed.'
                )
            )
        case Ok(_):
            flash_success(gettext('Match result has been corrected.'))
        case Err(error_message):
            flash_error(
                gettext(
                    'Error correcting match result: %(error)s Nothing '
                    'was changed; the original result remains '
                    'confirmed.',
                    error=gettext(error_message),
                )
            )

    return redirect_to('.view_match', match_id=match.id)


@blueprint.post('/orga/matches/<match_id>/set_ffa_placements')
@login_required
@scoped_orga_required
def orga_set_ffa_placements(match_id):
    """Set the placements of an FFA match."""
    match, tournament = _get_orga_match_and_tournament_or_404(match_id)

    if not _orga_results_editable(tournament):
        flash_error(gettext('Tournament is not in progress.'))
        return redirect_to('.view_match', match_id=match.id)

    if not is_ffa_tournament(tournament):
        flash_error(gettext('Placements apply only to free-for-all matches.'))
        return redirect_to('.view_match', match_id=match.id)

    parse_result = parse_submitted_ffa_placements(request.form)
    if parse_result.is_err():
        flash_error(parse_result.unwrap_err())
        return redirect_to('.view_match', match_id=match.id)

    match tournament_match_service.set_ffa_placements(
        match.id, parse_result.unwrap()
    ):
        case Ok(_):
            flash_success(gettext('Placements have been set.'))
        case Err(error_message):
            flash_error(
                gettext(
                    'Error setting placements: %(error)s',
                    error=gettext(error_message),
                )
            )

    return redirect_to('.view_match', match_id=match.id)


@blueprint.post('/orga/matches/<match_id>/confirm_ffa')
@login_required
@scoped_orga_required
def orga_confirm_ffa_match(match_id):
    """Confirm an FFA match once its placements are set."""
    match, tournament = _get_orga_match_and_tournament_or_404(match_id)

    if not _orga_results_editable(tournament):
        flash_error(gettext('Tournament is not in progress.'))
        return redirect_to('.view_match', match_id=match.id)

    if not is_ffa_tournament(tournament):
        flash_error(gettext('Placements apply only to free-for-all matches.'))
        return redirect_to('.view_match', match_id=match.id)

    match tournament_match_service.confirm_ffa_match(match.id, g.user.id):
        case Ok(_):
            flash_success(gettext('FFA match has been confirmed.'))
        case Err(error_message):
            flash_error(
                gettext(
                    'Error confirming FFA match: %(error)s',
                    error=gettext(error_message),
                )
            )

    return redirect_to('.view_match', match_id=match.id)


@blueprint.post('/orga/matches/<match_id>/add_comment')
@login_required
@scoped_orga_required
def orga_add_match_comment(match_id):
    """Add a comment to a match."""
    match, _tournament = _get_orga_match_and_tournament_or_404(match_id)

    comment = request.form.get('comment', '').strip()

    if not comment:
        flash_error(gettext('Comment cannot be empty.'))
        return redirect_to('.view_match', match_id=match_id)

    match tournament_match_service.add_comment(match.id, g.user.id, comment):
        case Ok(_):
            flash_success(gettext('Comment has been added.'))
        case Err(error_message):
            flash_error(
                gettext(
                    'Error adding comment: %(error)s',
                    error=gettext(error_message),
                )
            )

    return redirect_to('.view_match', match_id=match_id)


# Other transitions, such as cancelling, are reserved for global admins.
_ORGA_TOURNAMENT_STATUS_ACTIONS = {
    'start': TournamentStatus.ONGOING,
    'pause': TournamentStatus.PAUSED,
    'resume': TournamentStatus.ONGOING,
    'complete': TournamentStatus.COMPLETED,
}


@blueprint.post('/orga/tournaments/<tournament_id>/<action>')
@login_required
@scoped_orga_required
def orga_change_tournament_status(tournament_id, action):
    """Start, pause, resume, or complete the tournament."""
    tournament = _get_tournament_or_404(tournament_id)

    new_status = _ORGA_TOURNAMENT_STATUS_ACTIONS.get(action)
    if new_status is None:
        abort(404)

    # Reopening a completed tournament is an admin-only correction of
    # an orga's own mistake, so it must not be reachable from here.
    # Without this the `resume` action would be exactly that: it maps
    # to ONGOING, and COMPLETED -> ONGOING is now a valid transition
    # (see _VALID_STATUS_TRANSITIONS). The template only offers
    # Resume on a PAUSED tournament, but this route is a plain POST.
    if tournament.tournament_status == TournamentStatus.COMPLETED:
        flash_error(
            gettext(
                'A completed tournament can only be reopened by an '
                'administrator.'
            )
        )
        return redirect_to('.view', tournament_id=tournament.id)

    match tournament_service.change_status(
        tournament.id, new_status, g.user.id
    ):
        case Ok((_, _event)):
            flash_success(
                gettext(
                    'Tournament status has been changed to "%(status)s".',
                    status=new_status.name.replace('_', ' ').title(),
                )
            )
        case Err(error_message):
            flash_error(
                gettext(
                    'Status change failed: %(error)s',
                    error=gettext(error_message),
                )
            )

    return redirect_to('.view', tournament_id=tournament.id)


@blueprint.get('/<tournament_id>/bracket')
@templated
def bracket(tournament_id):
    """Show tournament bracket visualization."""
    tournament = _get_tournament_or_404(tournament_id)

    # Hide drafts from site visitors.
    if (
        tournament.tournament_status
        and tournament.tournament_status == TournamentStatus.DRAFT
    ):
        abort(404)

    matches = tournament_match_service.get_matches_for_tournament_ordered(
        tournament.id
    )

    # Bulk-fetch all contestants for the tournament in one query (not N).
    contestants_by_match = (
        tournament_match_service.get_contestants_for_tournament(tournament.id)
    )

    match_data = []
    all_contestants = []
    for match in matches:
        contestants = contestants_by_match.get(match.id, [])
        match_data.append(
            {
                'match': match,
                'contestants': contestants,
            }
        )
        all_contestants.append(contestants)

    # Fetch participants once, share across both helpers.
    participants = (
        tournament_participant_service.get_participants_for_tournament(
            tournament.id
        )
    )

    teams_by_id, participants_by_id = build_contestant_name_lookups(
        tournament.id, all_contestants, participants=participants
    )

    seats_by_user_id, team_members_by_team_id = build_hover_lookups(
        tournament,
        participants_by_id,
        teams_by_id,
        tournament.party_id,
        participants=participants,
    )

    # Tag matches that have pending feeders so the noscript template
    # can distinguish them from true structural defwins (bye matches).
    feed_counts = compute_feed_counts(match_data)
    for entry in match_data:
        m = entry['match']
        entry['has_pending_feeder'] = (
            feed_counts.get(str(m.id), 0) > 0
            and len(entry['contestants']) <= 1
            and not m.confirmed_by
        )

    # Bracket serialization for client-side rendering (SE/DE only).
    bracket_json = None
    if tournament.elimination_mode in (
        EliminationMode.SINGLE_ELIMINATION,
        EliminationMode.DOUBLE_ELIMINATION,
    ):
        from flask import url_for as flask_url_for

        bracket_json = serialize_bracket_json(
            tournament,
            match_data,
            teams_by_id,
            participants_by_id,
            seats_by_user_id,
            team_members_by_team_id,
            url_builder=lambda m: flask_url_for(
                '.view_match',
                tournament_id=tournament.id,
                match_id=m.id,
            ),
        )

    # Round-robin: compute standings table.
    standings = None
    if tournament.elimination_mode == EliminationMode.ROUND_ROBIN:
        standings = build_round_robin_standings(match_data)

    # FFA: compute cumulative standings with per-round breakdown.
    ffa_standings = None
    if tournament.game_format == GameFormat.FREE_FOR_ALL:
        ffa_standings = build_ffa_standings(match_data)

    return {
        'tournament': tournament,
        'match_data': match_data,
        'standings': standings,
        'ffa_standings': ffa_standings,
        'bracket_json': bracket_json,
        'teams_by_id': teams_by_id,
        'participants_by_id': participants_by_id,
        'seats_by_user_id': seats_by_user_id,
        'team_members_by_team_id': team_members_by_team_id,
        'active_tab': 'bracket',
    }


# -------------------------------------------------------------------- #
# highscore


@blueprint.get('/<tournament_id>/highscore')
@templated
def highscore(tournament_id, erroneous_form=None):
    """Show highscore leaderboard for the tournament."""
    tournament = _get_tournament_or_404(tournament_id)

    # Only HIGHSCORE tournaments have a leaderboard.
    if tournament.game_format != GameFormat.HIGHSCORE:
        abort(404)

    # Hide drafts from site visitors.
    if (
        tournament.tournament_status
        and tournament.tournament_status == TournamentStatus.DRAFT
    ):
        abort(404)

    party = party_service.get_party(tournament.party_id)

    leaderboard = []
    result = tournament_score_service.get_leaderboard(tournament.id)
    match result:
        case Ok(entries):
            leaderboard = entries
        case Err(e):
            flash_error(gettext(e))

    # Build name lookups for contestants.
    teams_by_id = {}
    participants_by_id = {}

    if tournament.contestant_type == ContestantType.TEAM:
        teams = tournament_team_service.get_teams_for_tournament(tournament.id)
        teams_by_id = {t.id: t for t in teams}
    else:
        participants = (
            tournament_participant_service.get_participants_for_tournament(
                tournament.id
            )
        )
        user_ids = {p.user_id for p in participants}
        users_by_id = user_service.get_users_indexed_by_id(user_ids)
        participants_by_id = {
            p.id: users_by_id[p.user_id]
            for p in participants
            if p.user_id in users_by_id and p.removed_at is None
        }

    form = erroneous_form if erroneous_form else HighscoreSubmitForm()

    return {
        'party': party,
        'tournament': tournament,
        'leaderboard': leaderboard,
        'participants_by_id': participants_by_id,
        'teams_by_id': teams_by_id,
        'active_tab': 'highscore',
        'form': form,
    }


@blueprint.post('/<tournament_id>/highscore/submit')
@login_required
def highscore_submit(tournament_id):
    """Submit the current user's own score to the highscore leaderboard."""
    tournament = _get_tournament_or_404(tournament_id)
    if tournament.game_format != GameFormat.HIGHSCORE:
        abort(404)
    if tournament.tournament_status not in (
        TournamentStatus.REGISTRATION_OPEN,
        TournamentStatus.ONGOING,
    ):
        flash_error(gettext('Tournament is not accepting score submissions.'))
        return redirect_to('.highscore', tournament_id=tournament_id)

    form = HighscoreSubmitForm(request.form)
    if not form.validate():
        return highscore(tournament_id, form)

    score = form.score.data
    note = form.note.data.strip() if form.note.data else None

    result = tournament_score_service.submit_score_by_participant(
        tournament.id, g.user.id, score, note=note
    )
    match result:
        case Ok(_):
            flash_success(gettext('Score submitted.'))
        case Err(error_message):
            flash_error(gettext(error_message))
    return redirect_to('.highscore', tournament_id=tournament_id)
