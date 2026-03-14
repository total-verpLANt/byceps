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
    build_hover_lookups,
    build_round_robin_standings,
    build_seat_lookup,
    serialize_bracket_json,
)
from byceps.services.lan_tournament.models.contestant_type import (
    ContestantType,
)
from byceps.services.lan_tournament.models.tournament_mode import (
    TournamentMode,
)
from byceps.services.party import party_service
from byceps.services.user import user_service
from byceps.util.framework.blueprint import create_blueprint
from byceps.util.framework.flash import flash_error, flash_success
from byceps.util.framework.templating import templated
from byceps.util.result import Err, Ok
from byceps.util.views import login_required, redirect_to


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

    participant_counts = {
        t.id: tournament_service.get_participant_count(t.id)
        for t in visible_tournaments
    }

    return {
        'tournaments': visible_tournaments,
        'participant_counts': participant_counts,
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

    # Resolve user names
    user_ids = {p.user_id for p in participants}
    users_by_id = user_service.get_users_indexed_by_id(user_ids)

    # Build seat lookup
    seats_by_user_id = build_seat_lookup(user_ids, tournament.party_id)

    return {
        'tournament': tournament,
        'participants': participants,
        'participant_count': participant_count,
        'current_user_participant': current_user_participant,
        'can_join': can_join,
        'can_leave': can_leave,
        'users_by_id': users_by_id,
        'seats_by_user_id': seats_by_user_id,
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

    teams = tournament_team_service.get_teams_for_tournament(tournament.id)

    return {
        'tournament': tournament,
        'teams': teams,
    }


@blueprint.get('/<tournament_id>/teams/create')
@login_required
@templated
def create_team_form(tournament_id):
    """Show form to create a team."""
    tournament = _get_tournament_or_404(tournament_id)

    if tournament.tournament_status != TournamentStatus.REGISTRATION_OPEN:
        flash_error(gettext('Registration is not open.'))
        return redirect_to('.view', tournament_id=tournament.id)

    return {
        'tournament': tournament,
    }


@blueprint.post('/<tournament_id>/teams/create')
@login_required
def create_team(tournament_id):
    """Create a team."""
    tournament = _get_tournament_or_404(tournament_id)

    if tournament.tournament_status != TournamentStatus.REGISTRATION_OPEN:
        flash_error(gettext('Registration is not open.'))
        return redirect_to('.view', tournament_id=tournament.id)

    name = request.form.get('name', '').strip()
    tag = request.form.get('tag', '').strip() or None
    description = request.form.get('description', '').strip() or None
    join_code = request.form.get('join_code', '').strip() or None

    if not name:
        flash_error(gettext('Team name is required.'))
        return redirect_to('.create_team_form', tournament_id=tournament.id)

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

    return {
        'tournament': tournament,
        'team': team,
        'team_members': team_members,
        'current_user_participant': current_user_participant,
        'is_team_member': is_team_member,
        'can_join_team': can_join_team,
        'can_leave_team': can_leave_team,
        'users_by_id': users_by_id,
        'seats_by_user_id': seats_by_user_id,
    }


@blueprint.post('/teams/<team_id>/join')
@login_required
def join_team(team_id):
    """Join a team."""
    team = _get_team_or_404(team_id)
    tournament = _get_tournament_or_404(team.tournament_id)

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

    return team


# -------------------------------------------------------------------- #
# matches


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
        'teams_by_id': teams_by_id,
        'participants_by_id': participants_by_id,
        'seats_by_user_id': seats_by_user_id,
        'team_members_by_team_id': team_members_by_team_id,
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
        role = tournament_match_service.MatchUserRole(None, False, False, False)
    current_user_contestant = role.contestant
    current_user_is_loser = role.is_loser
    current_user_can_confirm = role.can_confirm
    current_user_can_submit = role.can_submit

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
        flash_error(result.unwrap_err())
    else:
        flash_success(gettext('Match result submitted.'))
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

    # Bracket serialization for client-side rendering (SE/DE only).
    bracket_json = None
    if tournament.tournament_mode in (
        TournamentMode.SINGLE_ELIMINATION,
        TournamentMode.DOUBLE_ELIMINATION,
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
    if tournament.tournament_mode == TournamentMode.ROUND_ROBIN:
        standings = build_round_robin_standings(match_data)

    return {
        'tournament': tournament,
        'match_data': match_data,
        'standings': standings,
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
def highscore(tournament_id):
    """Show highscore leaderboard for the tournament."""
    tournament = _get_tournament_or_404(tournament_id)

    # Only HIGHSCORE tournaments have a leaderboard.
    if tournament.tournament_mode != TournamentMode.HIGHSCORE:
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
            flash_error(e)

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

    return {
        'party': party,
        'tournament': tournament,
        'leaderboard': leaderboard,
        'participants_by_id': participants_by_id,
        'teams_by_id': teams_by_id,
        'active_tab': 'highscore',
    }


@blueprint.post('/<tournament_id>/highscore/submit')
@login_required
def highscore_submit(tournament_id):
    """Submit the current user's own score to the highscore leaderboard."""
    tournament = _get_tournament_or_404(tournament_id)
    if tournament.tournament_mode != TournamentMode.HIGHSCORE:
        abort(404)
    if tournament.tournament_status not in (
        TournamentStatus.REGISTRATION_OPEN,
        TournamentStatus.ONGOING,
    ):
        flash_error(gettext('Tournament is not accepting score submissions.'))
        return redirect_to('.highscore', tournament_id=tournament_id)
    score_raw = request.form.get('score', '')
    note = request.form.get('note', '').strip() or None
    NOTE_MAX_LENGTH = 200
    if note is not None and len(note) > NOTE_MAX_LENGTH:
        flash_error(
            gettext(
                'Note must be %(max)d characters or fewer.', max=NOTE_MAX_LENGTH
            )
        )
        return redirect_to('.highscore', tournament_id=tournament_id)
    try:
        score = int(score_raw)
    except ValueError:
        flash_error(gettext('Invalid score value.'))
        return redirect_to('.highscore', tournament_id=tournament_id)
    if score < 0 or score > 999_999_999:
        flash_error(gettext('Score must be between 0 and 999,999,999.'))
        return redirect_to('.highscore', tournament_id=tournament_id)
    result = tournament_score_service.submit_score_by_participant(
        tournament.id, g.user.id, score, note=note
    )
    match result:
        case Ok(_):
            flash_success(gettext('Score submitted.'))
        case Err(error_message):
            flash_error(error_message)
    return redirect_to('.highscore', tournament_id=tournament_id)
