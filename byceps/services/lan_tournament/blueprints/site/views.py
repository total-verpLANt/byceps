from datetime import datetime, UTC
import re
import uuid
from flask import abort, g, jsonify, request
from flask_babel import gettext, to_user_timezone, to_utc

from byceps.services.lan_tournament import (
    tournament_domain_service,
    tournament_log_service,
    tournament_match_service,
    tournament_orga_service,
    tournament_participant_service,
    tournament_qualification_repository,
    tournament_qualification_service,
    tournament_request_domain_service,
    tournament_request_service,
    tournament_score_service,
    tournament_seeding_service,
    tournament_service,
    tournament_team_service,
)
from byceps.services.lan_tournament.models.bracket import Bracket
from byceps.services.lan_tournament.models.tournament import (
    Tournament,
    TournamentID,
)
from byceps.services.lan_tournament.models.tournament_match import (
    TournamentMatchID,
)
from byceps.services.lan_tournament.models.tournament_team import (
    TournamentTeam,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.lan_tournament.models.tournament_request import (
    TournamentRequest,
    TournamentRequestID,
    TournamentRequestStatus,
)
from byceps.services.lan_tournament.lan_tournament_view_helpers import (
    _resolve_contestant_name,
    build_contestant_name_lookups,
    build_ffa_standings,
    build_hover_lookups,
    build_round_robin_standings,
    build_seat_lookup,
    compute_feed_counts,
    contestant_names,
    ffa_elimination_mode,
    ffa_grand_final_offer,
    ffa_grand_final_refusal,
    ffa_phase,
    is_walkover_match,
    match_uses_placements,
    parse_match_ids,
    parse_int,
    parse_seeding_action,
    parse_submitted_contestant_scores,
    parse_submitted_ffa_placements,
    participant_rankings,
    phase_match_labels,
    plain_round_robin_winner_tie,
    playoff_origin_labels,
    playoff_waiting_reason,
    ffa_cut_ties_payload,
    qualification_js_strings,
    qualification_strings,
    SEEDING_LOG_PREFIXES,
    seeding_audit_rows,
    start_gate,
    start_refusal,
    seeding_board_payload,
    leaderboard_submission_times,
    seeding_error_status,
    separation_message,
    serialize_bracket_json,
    serialize_qualification,
    wants_json,
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
from byceps.services.ticketing import ticket_service
from byceps.services.user import user_service
from byceps.services.user.models import UserID
from byceps.util.framework.blueprint import create_blueprint
from byceps.util.framework.flash import (
    flash_error,
    flash_notice,
    flash_success,
)
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
    TournamentProposeForm,
    elimination_mode_label,
    game_format_label,
    request_mode_label,
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
        'start_gate': (
            start_gate(tournament) if may_administrate else None
        ),
        'grand_final': (
            ffa_grand_final_offer(tournament) if may_administrate else None
        ),
        'winner_tie': (
            plain_round_robin_winner_tie(tournament)
            if may_administrate
            and tournament.tournament_status
            in (TournamentStatus.ONGOING, TournamentStatus.PAUSED)
            else None
        ),
        'orgas': orgas,
        'ffa_phase': ffa_phase(tournament),
        'elimination_mode_label': (
            request_mode_label(tournament.elimination_mode)
            if tournament.elimination_mode
            else None
        ),
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
    """Abort with 404 if the tournament does not use teams.

    A missing `contestant_type` derives from team size rather than
    being treated as non-team outright, so a legacy tournament whose
    type was never set still gets the right gate.
    """
    contestant_type = tournament_domain_service.derive_contestant_type(
        tournament.contestant_type,
        tournament.max_players_in_team,
        tournament.min_players_in_team,
    )
    if contestant_type != ContestantType.TEAM:
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

    match tournament_team_service.remove_team_member(
        team.id, user_id, initiator_id=g.user.id
    ):
        case Ok(_event):
            flash_success(
                gettext('Member has been removed from the team.')
            )
        case Err(error_message):
            flash_error(
                gettext(
                    'Could not remove member: %(error)s',
                    error=gettext(error_message),
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
    is_ffa = match_uses_placements(tournament, match)
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
        'phase_lock': _phase_lock_of(tournament, match),
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


def _phase_lock_of(tournament, match):
    """Return `'final'` once a playoff result ends the release's undo."""
    if not (
        tournament.has_playoffs is True
        and tournament.playoff_released_at is not None
        and match.phase == 1
    ):
        return None
    progress = tournament_qualification_service.get_phase_two_progress(
        tournament.id
    )
    return 'final' if progress.has_result else 'released'


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

    if not match_uses_placements(tournament, match):
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

    if match_uses_placements(tournament, match):
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


@blueprint.post('/orga/matches/<match_id>/submit_ffa_result')
@login_required
@scoped_orga_required
def orga_submit_ffa_result(match_id):
    """Set the placements of an FFA match and confirm it, in one step.

    The panel offers a single button, so the two service calls are
    composed here rather than in the engine. They are deliberately
    not one transaction: a failed confirm leaves the placements
    stored but unconfirmed, which is exactly the state the former
    two-step flow produced between its two clicks. The panel renders
    that state with the selects pre-filled, so the same button
    retries it.

    The admin blueprint keeps the two steps separate; this is the
    site-side orga surface only.
    """
    match, tournament = _get_orga_match_and_tournament_or_404(match_id)

    if not _orga_results_editable(tournament):
        flash_error(gettext('Tournament is not in progress.'))
        return redirect_to('.view_match', match_id=match.id)

    if not match_uses_placements(tournament, match):
        flash_error(gettext('Placements apply only to free-for-all matches.'))
        return redirect_to('.view_match', match_id=match.id)

    parse_result = parse_submitted_ffa_placements(request.form)
    if parse_result.is_err():
        flash_error(parse_result.unwrap_err())
        return redirect_to('.view_match', match_id=match.id)

    set_result = tournament_match_service.set_ffa_placements(
        match.id, parse_result.unwrap()
    )
    if set_result.is_err():
        flash_error(
            gettext(
                'Error setting placements: %(error)s',
                error=gettext(set_result.unwrap_err()),
            )
        )
        return redirect_to('.view_match', match_id=match.id)

    match tournament_match_service.confirm_ffa_match(match.id, g.user.id):
        case Ok(_):
            flash_success(gettext('FFA match has been confirmed.'))
        case Err(error_message):
            flash_error(
                gettext(
                    'Error confirming FFA match: %(error)s The placements '
                    'were saved; submit again to confirm them.',
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

    if action == 'start':
        refusal = start_refusal(tournament, request.form)
        if refusal is not None:
            flash_error(gettext(refusal))
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


@blueprint.get('/orga/tournaments/<tournament_id>/seeding')
@login_required
@scoped_orga_required
@templated('site/lan_tournament/seeding')
def orga_seeding(tournament_id):
    """Show the seeding board of the tournament to its orgas."""
    tournament = _get_tournament_or_404(tournament_id)
    target = request.args.get(
        'target', tournament_seeding_service.INITIAL_TARGET
    )

    match tournament_seeding_service.get_board(
        tournament.id, target, initiator_id=g.user.id
    ):
        case Err(error_message):
            flash_error(gettext(error_message))
            return redirect_to('.view', tournament_id=tournament.id)
        case Ok(board):
            pass

    entries = [
        entry
        for entry in tournament_log_service.get_entries_for_tournament(
            tournament.id
        )
        if entry.event_type.startswith(SEEDING_LOG_PREFIXES)
    ]
    entries.reverse()
    users_by_id = user_service.get_users_indexed_by_id(
        {entry.initiator_id for entry in entries if entry.initiator_id}
    )

    return {
        'tournament': tournament,
        'board': seeding_board_payload(board),
        'audit_rows': seeding_audit_rows(
            entries, users_by_id, contestant_names(tournament.id)
        ),
    }


@blueprint.post('/orga/tournaments/<tournament_id>/seeding/actions')
@login_required
@scoped_orga_required
def orga_seeding_action(tournament_id):
    """Apply one seeding action; answer JSON or redirect."""
    tournament = _get_tournament_or_404(tournament_id)
    json_wanted = wants_json(request)
    target = request.form.get(
        'target', tournament_seeding_service.INITIAL_TARGET
    )

    parsed = parse_seeding_action(request.form)
    if parsed is None:
        return _seeding_error(
            tournament,
            target,
            tournament_seeding_service.ERR_INVALID_CHANGE,
            422,
            json_wanted,
        )
    expected_version, action = parsed

    match tournament_seeding_service.apply_action(
        tournament.id,
        target,
        action,
        expected_version=expected_version,
        initiator_id=g.user.id,
    ):
        case Err(error_message):
            return _seeding_error(
                tournament,
                target,
                error_message,
                seeding_error_status(error_message),
                json_wanted,
            )
        case Ok(board):
            if json_wanted:
                return jsonify(board=seeding_board_payload(board))

    return redirect_to(
        '.orga_seeding', tournament_id=tournament.id, target=target
    )


@blueprint.post('/orga/tournaments/<tournament_id>/seeding/generate')
@login_required
@scoped_orga_required
def orga_seeding_generate(tournament_id):
    """Generate the bracket, groups or lobbies from the seeding."""
    tournament = _get_tournament_or_404(tournament_id)
    target = request.form.get(
        'target', tournament_seeding_service.INITIAL_TARGET
    )

    version = parse_int(request.form.get('version'))
    if version is None:
        flash_error(gettext(tournament_seeding_service.ERR_INVALID_CHANGE))
        return redirect_to(
            '.orga_seeding', tournament_id=tournament.id, target=target
        )

    match tournament_seeding_service.generate_from_seeding(
        tournament.id,
        target,
        expected_version=version,
        initiator_id=g.user.id,
    ):
        case Ok('completed'):
            flash_success(gettext('The tournament is complete. The lone survivor wins.'))
            return redirect_to('.bracket', tournament_id=tournament.id)
        case Ok(tournament_seeding_service.GENERATION_UNCHANGED):
            flash_notice(gettext(tournament_seeding_service.MSG_UNCHANGED))
        case Ok(match_count):
            flash_success(
                gettext(
                    'Bracket generated with %(count)d matches.',
                    count=match_count,
                )
            )
        case Err(error_message):
            flash_error(gettext(error_message))

    return redirect_to(
        '.orga_seeding', tournament_id=tournament.id, target=target
    )


def _seeding_error(tournament, target, error_message, status, json_wanted):
    message = gettext(error_message)
    if json_wanted:
        return jsonify(error=message), status
    flash_error(message)
    return redirect_to(
        '.orga_seeding', tournament_id=tournament.id, target=target
    )


def _orga_qualification_payload(tournament, state):
    decisions = (
        tournament_qualification_repository.get_decisions_for_tournament(
            tournament.id
        )
    )
    user_ids = {b.decided_by for d in decisions.values() for b in d.blocks}
    if state.released_by is not None:
        user_ids.add(state.released_by)
    return serialize_qualification(
        state,
        contestant_names(tournament.id),
        qualification_strings(),
        tournament=tournament,
        decisions=decisions,
        users=user_service.get_users_indexed_by_id(user_ids),
        submitted_at=leaderboard_submission_times(tournament.id, state),
    )


def _playoff_release_open(tournament, state):
    return (
        state.ready
        and state.released_at is None
        and state.source != tournament_qualification_service.SOURCE_WINNER
        and tournament.tournament_status is TournamentStatus.ONGOING
    )


def _stored_playoff_draft_version(tournament):
    """Return the version of the stored playoff draft, or `None`.

    Read only: a missing draft is not created here.
    """
    match tournament_seeding_service.get_board(tournament.id, 'playoff'):
        case Ok(board):
            return board.version
        case Err(_):
            return None


@blueprint.get('/orga/tournaments/<tournament_id>/qualification')
@login_required
@scoped_orga_required
@templated('site/lan_tournament/qualification')
def orga_qualification(tournament_id):
    """Show who qualifies for the playoffs, the ties and the release."""
    tournament = _get_tournament_or_404(tournament_id)

    match tournament_qualification_service.get_qualification(tournament.id):
        case Err(error_message):
            flash_error(gettext(error_message))
            return redirect_to('.view', tournament_id=tournament.id)
        case Ok(state):
            pass

    entries = [
        entry
        for entry in tournament_log_service.get_entries_for_tournament(
            tournament.id
        )
        if entry.event_type.startswith(SEEDING_LOG_PREFIXES)
    ]
    entries.reverse()
    users_by_id = user_service.get_users_indexed_by_id(
        {entry.initiator_id for entry in entries if entry.initiator_id}
    )
    playoff_version = (
        _stored_playoff_draft_version(tournament)
        if _playoff_release_open(tournament, state)
        else None
    )

    return {
        'tournament': tournament,
        'qualification': _orga_qualification_payload(tournament, state),
        'playoff_version': playoff_version,
        'playoff_release_open': _playoff_release_open(tournament, state),
        'playoff_board': _playoff_draft_payload(state),
        'audit_rows': seeding_audit_rows(
            entries, users_by_id, contestant_names(tournament.id)
        ),
        'js_strings': qualification_js_strings(),
    }


@blueprint.post('/orga/tournaments/<tournament_id>/qualification/draft')
@login_required
@scoped_orga_required
def orga_qualification_draft_action(tournament_id):
    """Apply one seeding action to the playoff draft; answer JSON or redirect."""
    tournament = _get_tournament_or_404(tournament_id)
    json_wanted = wants_json(request)
    target = tournament_seeding_service.PLAYOFF_TARGET

    parsed = parse_seeding_action(request.form)
    if parsed is None:
        return _qualification_draft_error(
            tournament,
            tournament_seeding_service.ERR_INVALID_CHANGE,
            422,
            json_wanted,
        )
    expected_version, action = parsed

    before = tournament_seeding_service.get_board(tournament.id, target)
    match tournament_seeding_service.apply_action(
        tournament.id,
        target,
        action,
        expected_version=expected_version,
        initiator_id=g.user.id,
    ):
        case Err(error_message):
            return _qualification_draft_error(
                tournament,
                error_message,
                seeding_error_status(error_message),
                json_wanted,
            )
        case Ok(board):
            if json_wanted:
                return jsonify(board=seeding_board_payload(board))
            if isinstance(action, tournament_seeding_service.Separate):
                flash_success(
                    separation_message(
                        before.unwrap() if before.is_ok() else None, board
                    )
                )

    return redirect_to('.orga_qualification', tournament_id=tournament.id)


def _qualification_draft_error(tournament, error_message, status, json_wanted):
    message = gettext(error_message)
    if json_wanted:
        return jsonify(error=message), status
    flash_error(message)
    return redirect_to('.orga_qualification', tournament_id=tournament.id)


def _playoff_draft_payload(state):
    """Return the playoff draft board for the page, or None without one."""
    if state.source not in (
        tournament_qualification_service.SOURCE_GROUPS,
        tournament_qualification_service.SOURCE_LEADERBOARD,
    ):
        return None
    match tournament_seeding_service.get_board(
        state.tournament_id, tournament_seeding_service.PLAYOFF_TARGET
    ):
        case Ok(board):
            return seeding_board_payload(board)
        case Err(_):
            return None


def _orga_qualification_outcome(
    tournament,
    result,
    success_message,
    json_wanted,
    *,
    back='.orga_qualification',
):
    """Answer a qualification action: JSON state, or flash and redirect."""
    match result:
        case Err(error_message):
            status = seeding_error_status(error_message)
            if json_wanted:
                return jsonify(error=gettext(error_message)), status
            flash_error(gettext(error_message))
        case Ok(_):
            if json_wanted:
                state = tournament_qualification_service.get_qualification(
                    tournament.id
                )
                payload = (
                    _orga_qualification_payload(
                        _get_tournament_or_404(tournament.id), state.unwrap()
                    )
                    if state.is_ok()
                    else None
                )
                return jsonify(qualification=payload)
            flash_success(success_message)
    return redirect_to(back, tournament_id=tournament.id)


@blueprint.post('/orga/tournaments/<tournament_id>/qualification/decisions')
@login_required
@scoped_orga_required
def orga_qualification_decide(tournament_id):
    """Save or withdraw an orga decision on a tie."""
    tournament = _get_tournament_or_404(tournament_id)
    json_wanted = wants_json(request)
    scope = request.form.get('scope', '').strip()
    action = request.form.get('action', '')
    back = '.bracket' if scope.startswith('ffa:') else '.orga_qualification'

    if action == 'save':
        result = tournament_qualification_service.save_decision(
            tournament.id,
            scope,
            request.form.getlist('order'),
            reason=request.form.get('reason', ''),
            initiator_id=g.user.id,
        )
        message = gettext('Orga decision saved.')
    elif action == 'withdraw':
        result = tournament_qualification_service.withdraw_decision(
            tournament.id,
            scope,
            contestant_ids=request.form.getlist('order'),
            reason=request.form.get('reason', ''),
            initiator_id=g.user.id,
        )
        message = gettext('Decision withdrawn.')
    else:
        result = Err(tournament_seeding_service.ERR_INVALID_CHANGE)
        message = ''

    return _orga_qualification_outcome(
        tournament, result, message, json_wanted, back=back
    )


@blueprint.post(
    '/orga/tournaments/<tournament_id>/qualification/draft/create'
)
@login_required
@scoped_orga_required
def orga_qualification_draft_create(tournament_id):
    """Create the playoff draft once the qualification is ready."""
    tournament = _get_tournament_or_404(tournament_id)

    result = tournament_seeding_service.ensure_playoff_draft(
        tournament.id, g.user.id
    )

    return _orga_qualification_outcome(
        tournament,
        result,
        gettext('Playoff draft created.'),
        wants_json(request),
    )


@blueprint.post('/orga/tournaments/<tournament_id>/qualification/release')
@login_required
@scoped_orga_required
def orga_qualification_release(tournament_id):
    """Release the playoffs from the playoff draft."""
    tournament = _get_tournament_or_404(tournament_id)
    json_wanted = wants_json(request)

    version = parse_int(request.form.get('version'))
    if version is None:
        result = Err(tournament_seeding_service.ERR_INVALID_CHANGE)
    else:
        result = tournament_qualification_service.release_playoffs(
            tournament.id, expected_version=version, initiator_id=g.user.id
        )
    if result.is_ok():
        message = gettext(
            'Playoffs released with %(count)d matches.',
            count=result.unwrap(),
        )
    else:
        message = ''

    return _orga_qualification_outcome(tournament, result, message, json_wanted)


@blueprint.post('/orga/tournaments/<tournament_id>/qualification/unrelease')
@login_required
@scoped_orga_required
def orga_qualification_unrelease(tournament_id):
    """Take the release of the playoffs back, with a reason."""
    tournament = _get_tournament_or_404(tournament_id)

    result = tournament_qualification_service.unrelease_playoffs(
        tournament.id,
        reason=request.form.get('reason', ''),
        initiator_id=g.user.id,
    )

    return _orga_qualification_outcome(
        tournament,
        result,
        gettext('Release undone.'),
        wants_json(request),
    )


@blueprint.post('/orga/tournaments/<tournament_id>/leaderboard/close')
@login_required
@scoped_orga_required
def orga_leaderboard_close(tournament_id):
    """End the score phase of a highscore tournament."""
    tournament = _get_tournament_or_404(tournament_id)

    result = tournament_score_service.close_leaderboard(
        tournament.id, initiator_id=g.user.id
    )

    return _orga_qualification_outcome(
        tournament,
        result,
        gettext('Qualification closed.'),
        wants_json(request),
    )


@blueprint.post('/orga/tournaments/<tournament_id>/advance_ffa_round')
@login_required
@scoped_orga_required
def orga_advance_ffa_round(tournament_id):
    """Draft the next FFA round and send the orga to its seeding.

    For double elimination, the ``pool`` form field selects the pool to
    advance (``WB`` or ``LB``). The lobbies are generated from the draft,
    never here.
    """
    tournament = _get_tournament_or_404(tournament_id)

    elimination_mode = ffa_elimination_mode(tournament)
    if elimination_mode is None:
        flash_error(gettext('This tournament is not a Free-for-All format.'))
        return redirect_to('.view', tournament_id=tournament.id)

    pool = None
    if elimination_mode == EliminationMode.DOUBLE_ELIMINATION:
        pool_value = request.form.get('pool', '').strip()
        if pool_value == 'WB':
            pool = Bracket.WINNERS
        elif pool_value == 'LB':
            pool = Bracket.LOSERS
        else:
            flash_error(gettext('Invalid pool parameter. Use WB or LB.'))
            return redirect_to('.bracket', tournament_id=tournament.id)

    match tournament_seeding_service.prepare_ffa_round_draft(
        tournament.id, pool=pool, initiator_id=g.user.id
    ):
        case Ok('completed'):
            flash_success(gettext('The tournament is complete. The lone survivor wins.'))
            return redirect_to('.bracket', tournament_id=tournament.id)
        case Ok(target):
            return redirect_to(
                '.orga_seeding', tournament_id=tournament.id, target=target
            )
        case Err(error_message):
            flash_error(gettext(error_message))

    return redirect_to('.bracket', tournament_id=tournament.id)


@blueprint.post('/orga/tournaments/<tournament_id>/generate_ffa_grand_final')
@login_required
@scoped_orga_required
def orga_generate_ffa_grand_final(tournament_id):
    """Generate the grand final of a double-elimination FFA phase."""
    tournament = _get_tournament_or_404(tournament_id)

    refusal = ffa_grand_final_refusal(tournament)
    if refusal is not None:
        flash_error(gettext(refusal))
        return redirect_to('.view', tournament_id=tournament.id)

    match tournament_match_service.generate_ffa_grand_final(
        tournament.id, initiator_id=g.user.id
    ):
        case Ok(match_count):
            flash_success(
                gettext(
                    'Grand Final generated with %(count)d group(s).',
                    count=match_count,
                )
            )
            return redirect_to('.bracket', tournament_id=tournament.id)
        case Err(error_message):
            flash_error(
                gettext(
                    'Grand Final generation failed: %(error)s',
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

    may_administrate = may_administrate_tournament(g.user, tournament.id)

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

    # Playoff tournaments: the phase switch, group tables and waiting
    # state. Only what a participant may see goes into this context.
    phase_view = None
    waiting_reason = None
    rankings = None
    origin_labels: dict[str, str] = {}
    playoff_match_data = []
    playoff_matches = []
    playoff_lobby_rounds = []
    if tournament.has_playoffs:
        match tournament_qualification_service.get_qualification(tournament.id):
            case Ok(state) if state.source in ('groups', 'leaderboard'):
                rankings = participant_rankings(
                    state,
                    contestant_names(tournament.id),
                    qualification_strings(),
                    tournament,
                )
                waiting_reason = playoff_waiting_reason(state, tournament)
                origin_labels = playoff_origin_labels(state)
                released = state.released_at is not None
                phase_view = {'1': 1, '2': 2}.get(
                    request.args.get('phase', ''), 2 if released else 1
                )
                playoff_match_data = [
                    entry for entry in match_data if entry['match'].phase == 2
                ]
            case _:
                pass

    # Bracket serialization for client-side rendering (SE/DE only).
    bracket_json = None
    if phase_view is not None:
        playoff_elimination_mode = tournament.playoff_elimination_mode
        if (
            playoff_match_data
            and tournament.playoff_game_format != GameFormat.FREE_FOR_ALL
            and playoff_elimination_mode
            in (
                EliminationMode.SINGLE_ELIMINATION,
                EliminationMode.DOUBLE_ELIMINATION,
            )
        ):
            from flask import url_for as flask_url_for

            bracket_json = serialize_bracket_json(
                tournament,
                playoff_match_data,
                teams_by_id,
                participants_by_id,
                seats_by_user_id,
                team_members_by_team_id,
                url_builder=lambda m: flask_url_for(
                    '.view_match',
                    tournament_id=tournament.id,
                    match_id=m.id,
                ),
                origin_labels=origin_labels,
            )
    elif tournament.elimination_mode in (
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
    if (
        phase_view is None
        and tournament.elimination_mode == EliminationMode.ROUND_ROBIN
    ):
        standings = build_round_robin_standings(match_data)

    # FFA: compute cumulative standings with per-round breakdown.
    ffa_standings = None
    if phase_view is None and tournament.game_format == GameFormat.FREE_FOR_ALL:
        ffa_standings = build_ffa_standings(match_data)
    elif (
        phase_view == 2
        and tournament.playoff_game_format == GameFormat.FREE_FOR_ALL
    ):
        ffa_standings = build_ffa_standings(playoff_match_data)
        playoff_lobby_rounds = _playoff_lobby_rounds(
            playoff_match_data, teams_by_id, participants_by_id
        )

    if phase_view == 2 and bracket_json is not None:
        labels = phase_match_labels(
            tournament, [e['match'] for e in match_data]
        )
        playoff_matches = _playoff_match_rows(
            sorted(
                match_data,
                key=lambda e: (
                    e['match'].phase != 2,
                    e['match'].group_order or 0,
                ),
            ),
            labels,
            teams_by_id,
            participants_by_id,
        )

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
        'phase_view': phase_view,
        'waiting_reason': waiting_reason,
        'rankings': rankings,
        'playoff_matches': playoff_matches,
        'playoff_lobby_rounds': playoff_lobby_rounds,
        'active_tab': 'bracket',
        'ffa_cut_ties': (
            ffa_cut_ties_payload(tournament.id) if may_administrate else []
        ),
        'winner_tie': (
            plain_round_robin_winner_tie(tournament)
            if may_administrate
            and phase_view is None
            and tournament.tournament_status is TournamentStatus.ONGOING
            else None
        ),
        'js_strings': qualification_js_strings(),
    }


def _contestant_names_of(contestants, teams_by_id, participants_by_id):
    return sorted(
        (
            _resolve_contestant_name(c, teams_by_id, participants_by_id)
            for c in contestants
            if c.team_id or c.participant_id
        ),
        key=str.casefold,
    )


_LOBBY_POOL_ORDER = {'WB': 0, 'LB': 1, 'GF': 2}


def _playoff_lobby_rounds(match_data, teams_by_id, participants_by_id):
    """Return the phase-2 FFA lobbies by pool and round, members by name.

    Names are sorted, so the order of a lobby never tells a seed. A
    double-elimination tournament has a section per pool and round; the
    grand final has no round number.
    """

    def pool_of(match):
        return match.bracket.value if match.bracket else None

    groups: dict[tuple[str | None, int], list[dict]] = {}
    for entry in sorted(
        match_data,
        key=lambda e: (
            _LOBBY_POOL_ORDER.get(pool_of(e['match']), 0),
            e['match'].round or 0,
            e['match'].group_order or 0,
        ),
    ):
        match = entry['match']
        groups.setdefault((pool_of(match), match.round or 0), []).append(
            {
                'names': _contestant_names_of(
                    entry['contestants'], teams_by_id, participants_by_id
                ),
                'confirmed': match.confirmed_by is not None,
                'match_id': match.id,
            }
        )

    pool_labels = {
        'WB': gettext('Winners Pool'),
        'LB': gettext('Losers Pool'),
        'GF': gettext('Grand Final'),
    }
    rounds_seen: dict[str | None, int] = {}
    sections = []
    for (pool, _round), lobbies in groups.items():
        rounds_seen[pool] = rounds_seen.get(pool, 0) + 1
        sections.append(
            {
                'pool': pool_labels.get(pool),
                'number': None if pool == 'GF' else rounds_seen[pool],
                'lobbies': lobbies,
            }
        )
    return sections


def _playoff_match_rows(match_data, labels, teams_by_id, participants_by_id):
    """Return one row per match with its phase label, for the list."""
    rows = []
    for entry in match_data:
        match = entry['match']
        contestants = entry['contestants']
        real = [c for c in contestants if c.team_id or c.participant_id]
        confirmed = match.confirmed_by is not None
        rows.append(
            {
                'label': labels.get(str(match.id), ''),
                'names': [
                    _resolve_contestant_name(c, teams_by_id, participants_by_id)
                    for c in real
                ],
                'score': (
                    ':'.join(str(c.score) for c in real)
                    if confirmed and len(real) == 2
                    else None
                ),
                'status': (
                    'confirmed'
                    if confirmed
                    else 'ready'
                    if len(real) >= 2
                    else 'pending'
                ),
                'match_id': match.id,
            }
        )
    return rows


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


# -------------------------------------------------------------------- #
# tournament requests


@blueprint.get('/requests/propose')
@login_required
@templated
def propose_form(erroneous_form=None):
    """Show the form to propose a new tournament."""
    party = _get_current_party_or_404()

    form = erroneous_form if erroneous_form else TournamentProposeForm()
    form.set_format_choices()

    return {
        'mode': 'create',
        'form': form,
        'party': party,
        'party_capacity': party.max_ticket_quantity,
        'tournament_request': None,
        'history': None,
        'has_ticket': ticket_service.uses_any_ticket_for_party(
            g.user.id, party.id
        ),
    }


@blueprint.post('/requests/propose')
@login_required
def propose():
    """Submit a new tournament request."""
    party = _get_current_party_or_404()

    form = TournamentProposeForm(request.form)
    form.set_format_choices()

    has_ticket = ticket_service.uses_any_ticket_for_party(g.user.id, party.id)
    if not has_ticket:
        flash_error(
            gettext(
                'You must have a valid ticket for this party to propose '
                'a tournament.'
            )
        )
        return propose_form(form)

    if not form.validate():
        return propose_form(form)

    try:
        game_format = GameFormat(form.game_format.data)
        elimination_mode = EliminationMode(form.elimination_mode.data)
    except ValueError:
        flash_error(
            gettext('Invalid game format or elimination mode selected.')
        )
        return propose_form(form)

    special_rules = tournament_request_domain_service.normalize_optional_text(
        form.special_rules.data
    )
    notes = tournament_request_domain_service.normalize_optional_text(
        form.notes.data
    )
    desired_template = (
        tournament_request_domain_service.normalize_optional_text(
            form.desired_template.data
        )
    )

    match tournament_request_service.submit_request(
        party.id,
        g.user.id,
        party_capacity=party.max_ticket_quantity,
        name=form.name.data.strip(),
        game=form.game.data.strip(),
        game_format=game_format,
        elimination_mode=elimination_mode,
        team_size=form.team_size.data,
        participant_limit=form.participant_limit.data,
        preferred_start_time=to_utc(form.preferred_start_time.data),
        preferred_end_time=to_utc(form.preferred_end_time.data),
        description=form.description.data.strip(),
        special_rules=special_rules,
        notes=notes,
        desired_template=desired_template,
    ):
        case Ok((tournament_request, _event)):
            flash_success(
                gettext(
                    'Tournament request "%(name)s" has been submitted.',
                    name=tournament_request.name,
                )
            )
            return redirect_to('.my_requests')
        case Err(error_message):
            flash_error(gettext(error_message))
            return propose_form(form)


def _signup_counts(
    tournaments,
    participant_counts: dict[TournamentID, int],
    team_counts: dict[TournamentID, int],
) -> dict[TournamentID, tuple[int, int | None]]:
    """Map each tournament to its signup count and limit."""
    signup_counts = {}
    for tournament in tournaments:
        if tournament.contestant_type == ContestantType.TEAM:
            count = team_counts.get(tournament.id, 0)
            limit = tournament.max_teams
        else:
            count = participant_counts.get(tournament.id, 0)
            limit = tournament.max_players
        signup_counts[tournament.id] = (count, limit)
    return signup_counts


_REASON_LEAD_ABBREVIATIONS = (
    'z.',
    'u.',
    'z. B.',
    'z.B.',
    'u. a.',
    'u.a.',
    'd. h.',
    'ca.',
    'max.',
    'min.',
    'inkl.',
    'bzw.',
    'usw.',
    'Nr.',
    'evtl.',
    'ggf.',
)

# A sentence ends at `.!?` and whitespace before a capital, unless an ordinal
# or an abbreviation precedes it.
_REASON_LEAD_SPLIT = re.compile(
    r'(?<!\d\.)'
    + ''.join(
        rf'(?<!(?i:\b{re.escape(a)}))' for a in _REASON_LEAD_ABBREVIATIONS
    )
    + r'(?<=[.!?])\s+(?=["„»(]?[A-ZÄÖÜ])'
)


def _split_reason_lead(reason: str) -> tuple[str, bool]:
    """Return the first sentence of `reason` and whether more follows."""
    lead, *rest = _REASON_LEAD_SPLIT.split(reason.strip(), maxsplit=1)
    return lead, bool(rest)


@blueprint.get('/requests')
@login_required
@templated
def my_requests():
    """Show the current user's own tournament requests for this party."""
    party = _get_current_party_or_404()

    requests = tournament_request_service.get_visible_requests_for_user(
        party.id, g.user.id, is_admin=False
    )

    status_counts = {status.value: 0 for status in TournamentRequestStatus}
    for tournament_request in requests:
        status_counts[tournament_request.status.value] += 1

    open_requests = sorted(
        (
            r
            for r in requests
            if r.status
            in (
                TournamentRequestStatus.submitted,
                TournamentRequestStatus.accepted,
            )
        ),
        key=lambda r: (r.status != TournamentRequestStatus.submitted, r.number),
    )
    archived_requests = [
        r
        for r in requests
        if r.status
        in (
            TournamentRequestStatus.rejected,
            TournamentRequestStatus.withdrawn,
        )
    ]
    live_requests = [
        r
        for r in requests
        if r.status == TournamentRequestStatus.tournament_created
    ]

    tournaments_by_request_id = {}
    tournament_ids = []
    for tournament_request in live_requests:
        if tournament_request.created_tournament_id is None:
            continue
        tournament = tournament_service.find_tournament(
            tournament_request.created_tournament_id
        )
        if tournament is not None:
            tournaments_by_request_id[tournament_request.id] = tournament
            tournament_ids.append(tournament.id)

    participant_counts = (
        tournament_service.get_participant_counts_for_tournaments(
            tournament_ids
        )
    )
    team_counts = tournament_team_service.get_team_counts_for_tournaments(
        [
            tournament.id
            for tournament in tournaments_by_request_id.values()
            if tournament.contestant_type == ContestantType.TEAM
        ]
    )

    reason_leads_by_request_id = {
        tournament_request.id: _split_reason_lead(
            tournament_request.rejection_reason
        )
        for tournament_request in archived_requests
        if tournament_request.status == TournamentRequestStatus.rejected
        and tournament_request.rejection_reason
        and tournament_request.rejection_reason.strip()
    }

    # Site `view` 404s DRAFT tournaments for everyone, proposer included;
    # my_requests must not link to one it would only 404 on.
    draft_tournament_ids = {
        tournament.id
        for tournament in tournaments_by_request_id.values()
        if tournament.tournament_status == TournamentStatus.DRAFT
    }

    orga_tournament_ids = {
        tournament.id
        for tournament in tournaments_by_request_id.values()
        if tournament_orga_service.is_orga_for_tournament(
            g.user.id, tournament.id
        )
    }

    # The "waiting for the orga" hint needs an elapsed *calendar* day
    # count in the display timezone, not "under 24h" -- a request
    # submitted yesterday at 23:00 local and viewed today at 01:00
    # local is 1 day, not "today", even though under two hours have
    # passed. There is no such filter available to the plain-Jinja
    # template, so it is computed here instead.
    today_local = to_user_timezone(datetime.now(UTC)).date()
    waiting_days_by_request_id = {
        tournament_request.id: max(
            (
                today_local
                - to_user_timezone(
                    tournament_request_domain_service.normalize_datetime_to_utc(
                        tournament_request.created_at
                    )
                ).date()
            ).days,
            0,
        )
        for tournament_request in open_requests
        if tournament_request.status == TournamentRequestStatus.submitted
    }

    return {
        'requests': requests,
        'status_counts': status_counts,
        'open_requests': open_requests,
        'archived_requests': archived_requests,
        'live_requests': live_requests,
        'tournaments_by_request_id': tournaments_by_request_id,
        'participant_counts': participant_counts,
        'signup_counts': _signup_counts(
            tournaments_by_request_id.values(),
            participant_counts,
            team_counts,
        ),
        'reason_leads_by_request_id': reason_leads_by_request_id,
        'draft_tournament_ids': draft_tournament_ids,
        'orga_tournament_ids': orga_tournament_ids,
        'waiting_days_by_request_id': waiting_days_by_request_id,
    }


@blueprint.get('/requests/<request_id>/update')
@login_required
@templated('site/lan_tournament/propose_form')
def update_request_form(request_id, erroneous_form=None):
    """Show the form to edit an open tournament request.

    Renders the frozen, read-only variant instead once the request is
    no longer editable.
    """
    party = _get_current_party_or_404()
    tournament_request = _get_own_request_or_404(party, request_id)

    history = tournament_request_service.get_request_history(
        tournament_request.id
    )

    if not tournament_request.is_editable:
        created_tournament = None
        if tournament_request.created_tournament_id is not None:
            created_tournament = tournament_service.find_tournament(
                tournament_request.created_tournament_id
            )

        return {
            'mode': 'frozen',
            'tournament_request': tournament_request,
            'created_tournament': created_tournament,
            'elimination_mode_label': elimination_mode_label(
                tournament_request.elimination_mode
            ),
            'format_label': game_format_label(tournament_request.game_format),
            'request_mode_label': request_mode_label(
                tournament_request.elimination_mode
            ),
            'party': party,
            'party_capacity': party.max_ticket_quantity,
            'history': history,
            'form': None,
        }

    if erroneous_form is not None:
        form = erroneous_form
    else:
        form = TournamentProposeForm(
            data={
                'name': tournament_request.name,
                'game': tournament_request.game,
                'game_format': tournament_request.game_format.value,
                'elimination_mode': (
                    tournament_request.elimination_mode.value
                ),
                'team_size': tournament_request.team_size,
                'participant_limit': tournament_request.participant_limit,
                'preferred_start_time': to_user_timezone(
                    tournament_request.preferred_start_time
                ),
                'preferred_end_time': to_user_timezone(
                    tournament_request.preferred_end_time
                ),
                'description': tournament_request.description,
                'special_rules': tournament_request.special_rules or '',
                'notes': tournament_request.notes or '',
                'desired_template': (
                    tournament_request.desired_template or ''
                ),
            }
        )
    form.set_format_choices()

    return {
        'mode': 'edit',
        'tournament_request': tournament_request,
        'form': form,
        'party': party,
        'party_capacity': party.max_ticket_quantity,
        'history': history,
        'latest_changes': _latest_request_changes(history),
    }


_REQUEST_DATETIME_FIELDS = frozenset(
    {'preferred_start_time', 'preferred_end_time'}
)


def _latest_request_changes(history) -> dict[str, str]:
    """Map each field of the newest edit to its previous display value."""
    for entry in reversed(history):
        if entry.event_type != 'tournament-request-edited':
            continue

        previous_values = (entry.data or {}).get('previous_values')
        if not isinstance(previous_values, dict):
            return {}

        return {
            field: _previous_value_label(field, value)
            for field, value in previous_values.items()
        }

    return {}


def _previous_value_label(field: str, value: object) -> str:
    """Return a logged previous value the way the edit form shows it."""
    if value is None:
        return '\N{EM DASH}'

    if isinstance(value, str):
        try:
            if field in _REQUEST_DATETIME_FIELDS:
                moment = to_user_timezone(datetime.fromisoformat(value))
                return moment.strftime('%d.%m. %H:%M')
            if field == 'game_format':
                return game_format_label(GameFormat[value])
            if field == 'elimination_mode':
                return request_mode_label(EliminationMode[value])
        except (KeyError, ValueError):
            pass

    return str(value)


@blueprint.post('/requests/<request_id>/update')
@login_required
def update_request(request_id):
    """Update an open tournament request."""
    party = _get_current_party_or_404()
    tournament_request = _get_own_request_or_404(party, request_id)

    if not tournament_request.is_editable:
        flash_error(gettext('This request can no longer be edited.'))
        return redirect_to('.my_requests')

    form = TournamentProposeForm(request.form)
    form.set_format_choices()

    if not form.validate():
        return update_request_form(request_id, form)

    try:
        game_format = GameFormat(form.game_format.data)
        elimination_mode = EliminationMode(form.elimination_mode.data)
    except ValueError:
        flash_error(
            gettext('Invalid game format or elimination mode selected.')
        )
        return update_request_form(request_id, form)

    special_rules = tournament_request_domain_service.normalize_optional_text(
        form.special_rules.data
    )
    notes = tournament_request_domain_service.normalize_optional_text(
        form.notes.data
    )
    desired_template = (
        tournament_request_domain_service.normalize_optional_text(
            form.desired_template.data
        )
    )

    match tournament_request_service.update_request(
        TournamentRequestID(request_id),
        g.user.id,
        party_capacity=party.max_ticket_quantity,
        name=form.name.data.strip(),
        game=form.game.data.strip(),
        game_format=game_format,
        elimination_mode=elimination_mode,
        team_size=form.team_size.data,
        participant_limit=form.participant_limit.data,
        preferred_start_time=to_utc(form.preferred_start_time.data),
        preferred_end_time=to_utc(form.preferred_end_time.data),
        description=form.description.data.strip(),
        special_rules=special_rules,
        notes=notes,
        desired_template=desired_template,
    ):
        case Ok(_):
            flash_success(gettext('Tournament request has been updated.'))
            return redirect_to('.my_requests')
        case Err(error_message):
            flash_error(gettext(error_message))
            return update_request_form(request_id, form)


@blueprint.post('/requests/<request_id>/withdraw')
@login_required
def withdraw_request(request_id):
    """Withdraw an open tournament request."""
    party = _get_current_party_or_404()
    _get_own_request_or_404(party, request_id)

    match tournament_request_service.withdraw_request(
        TournamentRequestID(request_id), g.user.id
    ):
        case Ok(_):
            flash_success(gettext('Tournament request has been withdrawn.'))
        case Err(error_message):
            flash_error(gettext(error_message))

    return redirect_to('.my_requests')


def _get_own_request_or_404(party, request_id) -> TournamentRequest:
    """Return the request, or abort with 404 if it is not this user's own.

    `get_visible_requests_for_user` with `is_admin=False` already scopes
    to both the current party and the current user; a request that
    fails to show up there is either someone else's, some other
    party's, or does not exist at all -- all three are indistinguishable
    404s here, so the client-supplied ID is never trusted on its own.
    """
    try:
        uuid.UUID(str(request_id))
    except ValueError:
        abort(404)

    visible_requests = tournament_request_service.get_visible_requests_for_user(
        party.id, g.user.id, is_admin=False
    )
    for tournament_request in visible_requests:
        if str(tournament_request.id) == str(request_id):
            return tournament_request

    abort(404)
