import dataclasses

from flask import abort, g, request
from flask_babel import gettext, to_user_timezone, to_utc

from byceps.services.party import party_service
from byceps.services.party.models import Party
from byceps.util.framework.blueprint import create_blueprint
from byceps.util.framework.flash import flash_error, flash_success
from byceps.util.framework.templating import templated
from byceps.util.result import Err, Ok
from byceps.util.views import permission_required, redirect_to

from byceps.services.lan_tournament import (
    tournament_match_service,
    tournament_service,
    tournament_team_service,
)
from byceps.services.lan_tournament.models.tournament import Tournament
from byceps.services.lan_tournament.models.contestant_type import (
    ContestantType,
)
from byceps.services.lan_tournament.models.tournament_team import (
    TournamentTeam,
)
from byceps.services.lan_tournament.models.tournament_mode import (
    TournamentMode,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)

from .forms import (
    TeamCreateForm,
    TeamUpdateForm,
    TournamentCreateForm,
    TournamentUpdateForm,
)


blueprint = create_blueprint('lan_tournament_admin', __name__)


@blueprint.get('/for_party/<party_id>')
@permission_required('lan_tournament.view')
@templated
def index(party_id):
    """List tournaments for that party."""
    party = _get_party_or_404(party_id)

    tournaments = tournament_service.get_tournaments_for_party(party.id)

    return {
        'party': party,
        'tournaments': tournaments,
    }


@blueprint.get('/tournaments/<tournament_id>')
@permission_required('lan_tournament.view')
@templated
def view(tournament_id):
    """Show a tournament."""
    tournament = _get_tournament_or_404(tournament_id)

    party = party_service.get_party(tournament.party_id)

    return {
        'party': party,
        'tournament': tournament,
    }


@blueprint.get('/for_party/<party_id>/create')
@permission_required('lan_tournament.create')
@templated
def create_form(party_id, erroneous_form=None):
    """Show form to create a tournament."""
    party = _get_party_or_404(party_id)

    form = erroneous_form if erroneous_form else TournamentCreateForm()
    form.set_contestant_type_choices()
    form.set_tournament_mode_choices()

    return {
        'party': party,
        'form': form,
    }


@blueprint.post('/for_party/<party_id>')
@permission_required('lan_tournament.create')
def create(party_id):
    """Create a tournament."""
    party = _get_party_or_404(party_id)

    form = TournamentCreateForm(request.form)
    form.set_contestant_type_choices()
    form.set_tournament_mode_choices()

    if not form.validate():
        return create_form(party.id, form)

    name = form.name.data.strip()
    game = form.game.data.strip() if form.game.data else None
    description = (
        form.description.data.strip() if form.description.data else None
    )
    image_url = form.image_url.data.strip() if form.image_url.data else None
    ruleset = form.ruleset.data.strip() if form.ruleset.data else None
    start_time_local = form.start_time.data
    start_time = to_utc(start_time_local) if start_time_local else None
    try:
        contestant_type = (
            ContestantType[form.contestant_type.data]
            if form.contestant_type.data
            else None
        )
    except KeyError:
        flash_error(gettext('Invalid contestant type selected.'))
        return create_form(party.id, form)

    try:
        tournament_mode = (
            TournamentMode[form.tournament_mode.data]
            if form.tournament_mode.data
            else None
        )
    except KeyError:
        flash_error(gettext('Invalid tournament mode selected.'))
        return create_form(party.id, form)
    min_players = form.min_players.data
    max_players = form.max_players.data
    min_teams = form.min_teams.data
    max_teams = form.max_teams.data
    min_players_in_team = form.min_players_in_team.data
    max_players_in_team = form.max_players_in_team.data

    result = tournament_service.create_tournament(
        party.id,
        name,
        game=game,
        description=description,
        image_url=image_url,
        ruleset=ruleset,
        start_time=start_time,
        min_players=min_players,
        max_players=max_players,
        min_teams=min_teams,
        max_teams=max_teams,
        min_players_in_team=min_players_in_team,
        max_players_in_team=max_players_in_team,
        contestant_type=contestant_type,
        tournament_status=TournamentStatus.DRAFT,
        tournament_mode=tournament_mode,
    )
    if result.is_err():
        flash_error(result.unwrap_err())
        return create_form(party.id, form)

    tournament, _event = result.unwrap()

    flash_success(
        gettext(
            'Tournament "%(name)s" has been created.',
            name=tournament.name,
        )
    )

    return redirect_to('.view', tournament_id=tournament.id)


@blueprint.get('/tournaments/<tournament_id>/update')
@permission_required('lan_tournament.update')
@templated
def update_form(tournament_id, erroneous_form=None):
    """Show form to update the tournament."""
    tournament = _get_tournament_or_404(tournament_id)

    party = party_service.get_party(tournament.party_id)

    if erroneous_form:
        form = erroneous_form
    else:
        start_time_local = (
            to_user_timezone(tournament.start_time)
            if tournament.start_time
            else None
        )

        data = dataclasses.asdict(tournament)
        data['start_time'] = start_time_local
        if tournament.contestant_type is not None:
            data['contestant_type'] = tournament.contestant_type.name
        else:
            data['contestant_type'] = ''
        if tournament.tournament_mode is not None:
            data['tournament_mode'] = tournament.tournament_mode.name
        else:
            data['tournament_mode'] = ''
        form = TournamentUpdateForm(data=data)

    form.set_contestant_type_choices()
    form.set_tournament_mode_choices()

    return {
        'party': party,
        'tournament': tournament,
        'form': form,
    }


@blueprint.post('/tournaments/<tournament_id>')
@permission_required('lan_tournament.update')
def update(tournament_id):
    """Update the tournament."""
    tournament = _get_tournament_or_404(tournament_id)

    form = TournamentUpdateForm(request.form)
    form.set_contestant_type_choices()
    form.set_tournament_mode_choices()

    if not form.validate():
        return update_form(tournament.id, form)

    name = form.name.data.strip()
    game = form.game.data.strip() if form.game.data else None
    description = (
        form.description.data.strip() if form.description.data else None
    )
    image_url = form.image_url.data.strip() if form.image_url.data else None
    ruleset = form.ruleset.data.strip() if form.ruleset.data else None
    start_time_local = form.start_time.data
    start_time = to_utc(start_time_local) if start_time_local else None
    try:
        contestant_type = (
            ContestantType[form.contestant_type.data]
            if form.contestant_type.data
            else None
        )
    except KeyError:
        flash_error(gettext('Invalid contestant type selected.'))
        return update_form(tournament.id, form)

    try:
        tournament_mode = (
            TournamentMode[form.tournament_mode.data]
            if form.tournament_mode.data
            else None
        )
    except KeyError:
        flash_error(gettext('Invalid tournament mode selected.'))
        return update_form(tournament.id, form)
    min_players = form.min_players.data
    max_players = form.max_players.data
    min_teams = form.min_teams.data
    max_teams = form.max_teams.data
    min_players_in_team = form.min_players_in_team.data
    max_players_in_team = form.max_players_in_team.data

    result = tournament_service.update_tournament(
        tournament.id,
        name=name,
        game=game,
        description=description,
        image_url=image_url,
        ruleset=ruleset,
        start_time=start_time,
        min_players=min_players,
        max_players=max_players,
        min_teams=min_teams,
        max_teams=max_teams,
        min_players_in_team=min_players_in_team,
        max_players_in_team=max_players_in_team,
        contestant_type=contestant_type,
        tournament_mode=tournament_mode,
    )
    if result.is_err():
        flash_error(result.unwrap_err())
        return update_form(tournament.id, form)

    tournament = result.unwrap()

    flash_success(
        gettext(
            'Tournament "%(name)s" has been updated.',
            name=tournament.name,
        )
    )

    return redirect_to('.view', tournament_id=tournament.id)


@blueprint.post('/tournaments/<tournament_id>/delete')
@permission_required('lan_tournament.delete')
def delete(tournament_id):
    """Delete the tournament."""
    tournament = _get_tournament_or_404(tournament_id)

    tournament_service.delete_tournament(tournament.id)

    flash_success(
        gettext(
            'Tournament "%(name)s" has been deleted.',
            name=tournament.name,
        )
    )

    return redirect_to('.index', party_id=tournament.party_id)


@blueprint.post('/tournaments/<tournament_id>/open_registration')
@permission_required('lan_tournament.administrate')
def open_registration(tournament_id):
    """Open registration for the tournament."""
    return _change_status(tournament_id, TournamentStatus.REGISTRATION_OPEN)


@blueprint.post('/tournaments/<tournament_id>/close_registration')
@permission_required('lan_tournament.administrate')
def close_registration(tournament_id):
    """Close registration for the tournament."""
    return _change_status(tournament_id, TournamentStatus.REGISTRATION_CLOSED)


@blueprint.post('/tournaments/<tournament_id>/start')
@permission_required('lan_tournament.administrate')
def start(tournament_id):
    """Start the tournament."""
    return _change_status(tournament_id, TournamentStatus.ONGOING)


@blueprint.post('/tournaments/<tournament_id>/pause')
@permission_required('lan_tournament.administrate')
def pause(tournament_id):
    """Pause the tournament."""
    return _change_status(tournament_id, TournamentStatus.PAUSED)


@blueprint.post('/tournaments/<tournament_id>/resume')
@permission_required('lan_tournament.administrate')
def resume(tournament_id):
    """Resume the tournament."""
    return _change_status(tournament_id, TournamentStatus.ONGOING)


@blueprint.post('/tournaments/<tournament_id>/complete')
@permission_required('lan_tournament.administrate')
def complete(tournament_id):
    """Complete the tournament."""
    return _change_status(tournament_id, TournamentStatus.COMPLETED)


@blueprint.post('/tournaments/<tournament_id>/cancel')
@permission_required('lan_tournament.administrate')
def cancel(tournament_id):
    """Cancel the tournament."""
    return _change_status(tournament_id, TournamentStatus.CANCELLED)


def _change_status(tournament_id, new_status: TournamentStatus):
    """Change the tournament status."""
    tournament = _get_tournament_or_404(tournament_id)

    match tournament_service.change_status(tournament.id, new_status):
        case Ok((_, _event)):
            flash_success(
                gettext(
                    'Tournament status has been changed to "%(status)s".',
                    status=new_status.name,
                )
            )
        case Err(error_message):
            flash_error(
                gettext(
                    'Status change failed: %(error)s',
                    error=error_message,
                )
            )

    return redirect_to('.view', tournament_id=tournament.id)


@blueprint.post('/tournaments/<tournament_id>/generate_bracket')
@permission_required('lan_tournament.administrate')
def generate_bracket(tournament_id):
    """Generate bracket for the tournament."""
    tournament = _get_tournament_or_404(tournament_id)

    try:
        seeds = tournament_match_service.generate_single_elimination_bracket(
            tournament.id
        )
        flash_success(
            gettext(
                'Bracket generated with %(count)d matches.',
                count=len(seeds),
            )
        )
    except ValueError as e:
        flash_error(
            gettext(
                'Bracket generation failed: %(error)s',
                error=str(e),
            )
        )

    return redirect_to('.view', tournament_id=tournament.id)


# -------------------------------------------------------------------- #
# teams


@blueprint.get('/tournaments/<tournament_id>/teams')
@permission_required('lan_tournament.view')
@templated
def teams_for_tournament(tournament_id):
    """List teams for that tournament."""
    tournament = _get_tournament_or_404(tournament_id)
    party = party_service.get_party(tournament.party_id)

    teams = tournament_team_service.get_teams_for_tournament(tournament.id)

    return {
        'party': party,
        'tournament': tournament,
        'teams': teams,
    }


@blueprint.get('/tournaments/<tournament_id>/teams/create')
@permission_required('lan_tournament.create')
@templated
def create_team_form(tournament_id, erroneous_form=None):
    """Show form to create a team."""
    tournament = _get_tournament_or_404(tournament_id)
    party = party_service.get_party(tournament.party_id)

    form = erroneous_form if erroneous_form else TeamCreateForm()

    return {
        'party': party,
        'tournament': tournament,
        'form': form,
    }


@blueprint.post('/tournaments/<tournament_id>/teams')
@permission_required('lan_tournament.create')
def create_team(tournament_id):
    """Create a team."""
    tournament = _get_tournament_or_404(tournament_id)

    form = TeamCreateForm(request.form)

    if not form.validate():
        return create_team_form(tournament.id, form)

    name = form.name.data.strip()
    tag = form.tag.data.strip() if form.tag.data else None
    description = (
        form.description.data.strip() if form.description.data else None
    )
    image_url = form.image_url.data.strip() if form.image_url.data else None
    join_code = form.join_code.data.strip() if form.join_code.data else None

    captain_user_id = g.user.id

    match tournament_team_service.create_team(
        tournament.id,
        name,
        captain_user_id,
        tag=tag,
        description=description,
        image_url=image_url,
        join_code=join_code,
    ):
        case Ok((team, _event)):
            flash_success(
                gettext(
                    'Team "%(name)s" has been created.',
                    name=team.name,
                )
            )
            return redirect_to(
                '.teams_for_tournament', tournament_id=tournament.id
            )
        case Err(error_message):
            flash_error(
                gettext(
                    'Team creation failed: %(error)s',
                    error=error_message,
                )
            )
            return create_team_form(tournament.id, form)


@blueprint.get('/teams/<team_id>/update')
@permission_required('lan_tournament.update')
@templated
def update_team_form(team_id, erroneous_form=None):
    """Show form to update a team."""
    team = _get_team_or_404(team_id)
    tournament = _get_tournament_or_404(team.tournament_id)
    party = party_service.get_party(tournament.party_id)

    if erroneous_form:
        form = erroneous_form
    else:
        data = dataclasses.asdict(team)
        form = TeamUpdateForm(data=data)

    return {
        'party': party,
        'tournament': tournament,
        'team': team,
        'form': form,
    }


@blueprint.post('/teams/<team_id>')
@permission_required('lan_tournament.update')
def update_team(team_id):
    """Update a team."""
    team = _get_team_or_404(team_id)
    tournament = _get_tournament_or_404(team.tournament_id)

    form = TeamUpdateForm(request.form)

    if not form.validate():
        return update_team_form(team.id, form)

    name = form.name.data.strip()
    tag = form.tag.data.strip() if form.tag.data else None
    description = (
        form.description.data.strip() if form.description.data else None
    )
    image_url = form.image_url.data.strip() if form.image_url.data else None
    join_code = form.join_code.data.strip() if form.join_code.data else None

    result = tournament_team_service.update_team(
        team.id,
        name=name,
        tag=tag,
        description=description,
        image_url=image_url,
        join_code=join_code,
    )
    if result.is_err():
        flash_error(result.unwrap_err())
        return update_team_form(team.id, form)

    updated_team = result.unwrap()

    flash_success(
        gettext(
            'Team "%(name)s" has been updated.',
            name=updated_team.name,
        )
    )

    return redirect_to('.teams_for_tournament', tournament_id=tournament.id)


@blueprint.post('/teams/<team_id>/delete')
@permission_required('lan_tournament.delete')
def delete_team(team_id):
    """Delete a team."""
    team = _get_team_or_404(team_id)
    tournament_id = team.tournament_id

    match tournament_team_service.delete_team(team.id):
        case Ok(_event):
            flash_success(
                gettext(
                    'Team "%(name)s" has been deleted.',
                    name=team.name,
                )
            )
        case Err(error_message):
            flash_error(
                gettext(
                    'Team deletion failed: %(error)s',
                    error=error_message,
                )
            )

    return redirect_to('.teams_for_tournament', tournament_id=tournament_id)


def _get_party_or_404(party_id) -> Party:
    party = party_service.find_party(party_id)

    if party is None:
        abort(404)

    return party


def _get_tournament_or_404(tournament_id) -> Tournament:
    tournament = tournament_service.find_tournament(tournament_id)

    if tournament is None:
        abort(404)

    return tournament


def _get_team_or_404(team_id) -> TournamentTeam:
    team = tournament_team_service.get_team(team_id)

    if team is None:
        abort(404)

    return team



# -------------------------------------------------------------------- #
# matches


@blueprint.get('/tournaments/<tournament_id>/matches')
@permission_required('lan_tournament.view')
@templated
def matches_for_tournament(tournament_id):
    """List matches for that tournament."""
    tournament = _get_tournament_or_404(tournament_id)
    party = party_service.get_party(tournament.party_id)

    matches = tournament_match_service.get_matches_for_tournament(
        tournament.id
    )

    # Get contestants for each match
    from byceps.services.lan_tournament import tournament_repository

    match_data = []
    for match in matches:
        contestants = tournament_repository.get_contestants_for_match(
            match.id
        )
        match_data.append({
            'match': match,
            'contestants': contestants,
        })

    return {
        'party': party,
        'tournament': tournament,
        'match_data': match_data,
    }


@blueprint.get('/matches/<match_id>')
@permission_required('lan_tournament.view')
@templated
def view_match(match_id):
    """Show a match."""
    from byceps.services.lan_tournament import tournament_repository
    from byceps.services.lan_tournament.models.tournament_match import (
        TournamentMatchID,
    )

    match_id_obj = TournamentMatchID(match_id)
    match = tournament_match_service.get_match(match_id_obj)
    tournament = _get_tournament_or_404(match.tournament_id)
    party = party_service.get_party(tournament.party_id)

    contestants = tournament_repository.get_contestants_for_match(
        match_id_obj
    )
    comments = tournament_match_service.get_comments_from_match(match_id_obj)

    return {
        'party': party,
        'tournament': tournament,
        'match': match,
        'contestants': contestants,
        'comments': comments,
    }


@blueprint.post('/matches/<match_id>/set_score')
@permission_required('lan_tournament.update')
def set_match_score(match_id):
    """Set score for a contestant in a match."""
    from byceps.services.lan_tournament.models.tournament_match import (
        TournamentMatchID,
    )

    match_id_obj = TournamentMatchID(match_id)
    match = tournament_match_service.get_match(match_id_obj)

    contestant_id = request.form.get('contestant_id', '').strip()
    score = request.form.get('score', '').strip()

    if not contestant_id or not score:
        flash_error(gettext('Contestant ID and score are required.'))
        return redirect_to('.view_match', match_id=match_id)

    try:
        score_int = int(score)
        from uuid import UUID

        # Try to parse as participant or team ID
        from byceps.services.lan_tournament.models.tournament_participant \
            import TournamentParticipantID
        from byceps.services.lan_tournament.models.tournament_team import (
            TournamentTeamID,
        )

        contestant_uuid = UUID(contestant_id)

        # Determine if it's a participant or team based on tournament type
        tournament = _get_tournament_or_404(match.tournament_id)
        from byceps.services.lan_tournament.models.contestant_type import (
            ContestantType,
        )

        if tournament.contestant_type == ContestantType.TEAM:
            contestant_id_obj = TournamentTeamID(contestant_uuid)
        else:
            contestant_id_obj = TournamentParticipantID(contestant_uuid)

        tournament_match_service.set_score(
            match_id_obj, contestant_id_obj, score_int
        )

        flash_success(gettext('Score has been set.'))
    except ValueError as e:
        flash_error(gettext('Error setting score: %(error)s', error=str(e)))

    return redirect_to('.view_match', match_id=match_id)


@blueprint.post('/matches/<match_id>/confirm')
@permission_required('lan_tournament.administrate')
def confirm_match(match_id):
    """Confirm a match result."""
    from byceps.services.lan_tournament.models.tournament_match import (
        TournamentMatchID,
    )

    match_id_obj = TournamentMatchID(match_id)

    try:
        tournament_match_service.confirm_match(match_id_obj, g.user.id)
        flash_success(gettext('Match has been confirmed.'))
    except ValueError as e:
        flash_error(
            gettext('Error confirming match: %(error)s', error=str(e))
        )

    return redirect_to('.view_match', match_id=match_id)


@blueprint.post('/matches/<match_id>/add_comment')
@permission_required('lan_tournament.update')
def add_match_comment(match_id):
    """Add a comment to a match."""
    from byceps.services.lan_tournament.models.tournament_match import (
        TournamentMatchID,
    )

    match_id_obj = TournamentMatchID(match_id)

    comment = request.form.get('comment', '').strip()

    if not comment:
        flash_error(gettext('Comment cannot be empty.'))
        return redirect_to('.view_match', match_id=match_id)

    try:
        tournament_match_service.add_comment(
            match_id_obj, g.user.id, comment
        )
        flash_success(gettext('Comment has been added.'))
    except ValueError as e:
        flash_error(gettext('Error adding comment: %(error)s', error=str(e)))

    return redirect_to('.view_match', match_id=match_id)





@blueprint.get('/tournaments/<tournament_id>/bracket')
@permission_required('lan_tournament.view')
@templated
def bracket(tournament_id):
    """Show tournament bracket visualization."""
    tournament = _get_tournament_or_404(tournament_id)
    party = party_service.get_party(tournament.party_id)

    matches = tournament_match_service.get_matches_for_tournament(
        tournament.id
    )

    # Get contestants for each match
    from byceps.services.lan_tournament import tournament_repository

    match_data = []
    for match in matches:
        contestants = tournament_repository.get_contestants_for_match(
            match.id
        )
        match_data.append({
            'match': match,
            'contestants': contestants,
        })

    return {
        'party': party,
        'tournament': tournament,
        'match_data': match_data,
    }
@blueprint.post('/matches/<match_id>/comments/<comment_id>/delete')
@permission_required('lan_tournament.administrate')
def delete_match_comment(match_id, comment_id):
    """Delete a match comment."""
    from byceps.services.lan_tournament.models.tournament_match_comment \
        import TournamentMatchCommentID

    comment_id_obj = TournamentMatchCommentID(comment_id)

    try:
        tournament_match_service.delete_comment(comment_id_obj)
        flash_success(gettext('Comment has been deleted.'))
    except ValueError as e:
        flash_error(
            gettext('Error deleting comment: %(error)s', error=str(e))
        )

    return redirect_to('.view_match', match_id=match_id)
