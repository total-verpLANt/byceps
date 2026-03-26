import uuid
from datetime import datetime
from flask import abort, g, request
from flask_babel import gettext

from byceps.services.lan_tournament import (
    tournament_match_service,
    tournament_participant_service,
    tournament_score_service,
    tournament_service,
    tournament_team_service,
)
from byceps.services.lan_tournament.models.tournament import Tournament
from byceps.services.lan_tournament.models.tournament_team import TournamentTeam
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
    serialize_bracket_json,
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

from .forms import (
    HighscoreSubmitForm,
    MatchCommentForm,
    SiteTeamCreateForm,
    SiteTeamUpdateForm,
)


blueprint = create_blueprint('lan_tournament', __name__)


_EPOCH = datetime.min


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

    # Sort: registration open first, then by start time.
    def _sort_key(t: Tournament) -> tuple:
        is_registration_open = (
            t.tournament_status == TournamentStatus.REGISTRATION_OPEN
        )
        return (not is_registration_open, t.start_time or _EPOCH)

    visible_tournaments.sort(key=_sort_key)

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
