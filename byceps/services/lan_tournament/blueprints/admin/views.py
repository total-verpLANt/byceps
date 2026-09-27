from collections import Counter
import dataclasses
from datetime import datetime, UTC
from uuid import UUID

from flask import abort, g, request, url_for
from flask_babel import gettext, to_user_timezone, to_utc

from byceps.services.brand import brand_service
from byceps.services.party import party_service
from byceps.services.party.models import Party, PartyID
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
from byceps.util.views import permission_required, redirect_to, respond_no_content

from byceps.services.lan_tournament import (
    tournament_domain_service,
    tournament_match_service,
    tournament_notification_service,
    tournament_orga_service,
    tournament_participant_service,
    tournament_request_domain_service,
    tournament_request_repository,
    tournament_request_service,
    tournament_score_service,
    tournament_service,
    tournament_stats_service,
    tournament_team_service,
)
from byceps.services.lan_tournament.models.tournament import (
    Tournament,
    TournamentID,
)
from byceps.services.lan_tournament.models.tournament_request import (
    TournamentRequest,
    TournamentRequestID,
    TournamentRequestStatus,
)
from byceps.services.lan_tournament.models.contestant_type import (
    ContestantType,
)
from byceps.services.lan_tournament.models.tournament_team import (
    TournamentTeam,
    TournamentTeamID,
)
from byceps.services.lan_tournament.models.tournament_match import (
    TournamentMatch,
    TournamentMatchID,
)
from byceps.services.lan_tournament.models.tournament_match_comment import (
    TournamentMatchCommentID,
)
from byceps.services.lan_tournament.models.score_ordering import ScoreOrdering
from byceps.services.lan_tournament.models.bracket import Bracket
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import (
    GameFormat,
    is_valid_combination,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.lan_tournament.models.ffa_de_pool_data import (
    FfaDePoolData,
)
from byceps.services.lan_tournament.tournament_match_service import (
    acknowledgement_match_ids,
    retraction_reverts_completion,
)
from byceps.services.lan_tournament.tournament_service import (
    EDIT_LOCKED_STATUSES,
    resolve_podium_display_names,
    resolve_winner_display_name,
)
from byceps.services.lan_tournament.lan_tournament_view_helpers import (
    build_contestant_name_lookups,
    build_downstream_impact,
    build_hover_lookups,
    build_match_label,
    build_round_robin_standings,
    build_seat_lookup,
    build_team_members_lookup,
    compute_feed_counts,
    is_ffa_tournament,
    is_walkover_match,
    parse_match_ids,
    parse_submitted_contestant_scores,
    parse_submitted_ffa_placements,
)
from byceps.services.more.blueprints.admin import item_service
from byceps.services.more.blueprints.admin.item_service import MoreItem

from .forms import (
    AddParticipantForm,
    AddTeamMemberForm,
    HighscoreSubmitForm,
    MatchCorrectionForm,
    MatchUnconfirmForm,
    TeamCreateForm,
    TeamUpdateForm,
    TransferCaptainForm,
    TournamentCreateForm,
    TournamentOrgaAssignForm,
    TournamentRequestRejectForm,
    TournamentRequestUpdateForm,
    TournamentUpdateForm,
    _REQUEST_ELIMINATION_MODE_LABELS,
)


blueprint = create_blueprint('lan_tournament_admin', __name__)


# --- Monkey-patch "More" party items to include LAN Tournaments ---
if not getattr(item_service.get_party_items, '_lan_tournament_patched', False):
    _original_get_party_items = item_service.get_party_items

    def _get_party_items_with_lan_tournaments(party):
        items = _original_get_party_items(party)
        items = [
            item for item in items if item.required_permission != 'tourney.view'
        ]
        items.append(
            MoreItem(
                label=gettext('LAN Tournaments'),
                icon='trophy',
                url=url_for('lan_tournament_admin.overview', party_id=party.id),
                required_permission='lan_tournament.view',
            )
        )
        return items

    _get_party_items_with_lan_tournaments._lan_tournament_patched = True
    item_service.get_party_items = _get_party_items_with_lan_tournaments


@blueprint.get('/for_party/<party_id>/overview')
@permission_required('lan_tournament.view')
@templated
def overview(party_id):
    """Show tournament overview dashboard for a party."""
    party = _get_party_or_404(party_id)
    tournaments = tournament_service.get_tournaments_for_party(party.id)
    participant_counts = (
        tournament_service.get_participant_counts_for_tournaments(
            [t.id for t in tournaments]
        )
    )
    stats = tournament_stats_service.get_stats_for_party(
        tournaments, participant_counts
    )

    brand = brand_service.get_brand(party.brand_id)
    email_templates_configured = tournament_notification_service.email_templates_exist(brand)

    return {
        'party': party,
        'tournaments': tournaments,
        'stats': stats,
        'participant_counts': participant_counts,
        'email_templates_configured': email_templates_configured,
    }


@blueprint.get('/for_party/<party_id>')
@permission_required('lan_tournament.view')
@templated
def index(party_id):
    """List tournaments for that party."""
    party = _get_party_or_404(party_id)

    tournaments = tournament_service.get_tournaments_for_party(party.id)

    tournament_ids = [t.id for t in tournaments]
    participant_counts = tournament_service.get_participant_counts_for_tournaments(tournament_ids)
    team_counts = tournament_team_service.get_team_counts_for_tournaments(tournament_ids)

    return {
        'party': party,
        'tournaments': tournaments,
        'participant_counts': participant_counts,
        'team_counts': team_counts,
    }


@blueprint.post('/for_party/<party_id>/sort')
@permission_required('lan_tournament.update')
@respond_no_content
def sort_tournaments(party_id):
    """Reorder tournaments for that party."""
    _get_party_or_404(party_id)

    data = request.get_json(silent=True)
    if data is None or 'tournament_ids' not in data:
        abort(400)

    tournament_ids = data['tournament_ids']
    if not isinstance(tournament_ids, list):
        abort(400)

    if not all(isinstance(tid, str) for tid in tournament_ids):
        abort(400)

    try:
        tournament_service.reorder_tournaments(party_id, tournament_ids)
    except ValueError:
        abort(400)


@blueprint.get('/tournaments/<tournament_id>')
@permission_required('lan_tournament.view')
@templated
def view(tournament_id):
    """Show a tournament."""
    tournament = _get_tournament_or_404(tournament_id)

    party = party_service.get_party(tournament.party_id)

    # Check if tournament has bracket
    has_bracket = tournament_match_service.has_matches(tournament.id)

    is_team_tournament = tournament.contestant_type == ContestantType.TEAM

    participant_counts = (
        tournament_service.get_participant_counts_for_tournaments(
            [tournament.id]
        )
    )
    participant_count = participant_counts.get(tournament.id, 0)

    teams = []
    team_count = 0
    teams_below_minimum = []
    member_counts = {}
    if is_team_tournament:
        teams = tournament_team_service.get_teams_for_tournament(tournament.id)
        team_count = len(teams)
        member_counts = tournament_team_service.get_team_member_counts(
            tournament.id
        )
        teams_below_minimum = (
            tournament_participant_service.get_teams_below_minimum_size(
                tournament.id, tournament=tournament
            )
        )

    winner_name = resolve_winner_display_name(tournament)
    podium = resolve_podium_display_names(tournament)
    runner_up_name = podium.get('runner_up')
    bronze_name = podium.get('bronze')

    # FFA-DE: compute pool status for overview display.
    ffa_de_pool_status = None
    ffa_gf_eligible = False
    is_ffa_de = (
        tournament.game_format == GameFormat.FREE_FOR_ALL
        and tournament.elimination_mode
        == EliminationMode.DOUBLE_ELIMINATION
    )
    if is_ffa_de and has_bracket:
        pool_data = _build_ffa_de_pool_data(
            tournament.id,
            _build_ffa_match_data_list(tournament.id),
        )
        gf_exists = pool_data.gf_exists
        ffa_de_pool_status = pool_data.pool_status
        # GF eligible if both pools have confirmed latest rounds
        # and no GF yet.
        ffa_gf_eligible = (
            pool_data.wb_all_confirmed
            and pool_data.lb_all_confirmed
            and not gf_exists
            and pool_data.wb_latest_round is not None
            and pool_data.lb_latest_round is not None
        )

    return {
        'party': party,
        'tournament': tournament,
        'has_bracket': has_bracket,
        'requires_bracket': tournament.game_format.requires_bracket_generation if tournament.game_format else False,
        'is_team_tournament': is_team_tournament,
        'participant_count': participant_count,
        'team_count': team_count,
        'teams': teams,
        'member_counts': member_counts,
        'teams_below_minimum': teams_below_minimum,
        'winner_name': winner_name,
        'runner_up_name': runner_up_name,
        'bronze_name': bronze_name,
        'ffa_de_pool_status': ffa_de_pool_status,
        'ffa_gf_eligible': ffa_gf_eligible,
        'active_tab': 'overview',
    }


@blueprint.get('/for_party/<party_id>/create')
@permission_required('lan_tournament.create')
@templated
def create_form(party_id, erroneous_form=None):
    """Show form to create a tournament."""
    party = _get_party_or_404(party_id)

    if erroneous_form:
        form = erroneous_form
    else:
        form = TournamentCreateForm()

        from_request_raw = request.args.get('from_request')
        if from_request_raw:
            if not (
                g.user.has_permission('lan_tournament.request_view')
                and g.user.has_permission('lan_tournament.request_decide')
            ):
                # Prefilling needs both permissions, in lockstep with
                # `create`'s own from-request branch below: submitting
                # a prefilled form is what actually consumes the
                # request (a decision), so a `request_view`-only admin
                # must not be shown a prefill that submit will refuse
                # anyway -- that refusal clears the hidden field, and a
                # second submit would then create an unlinked
                # tournament while the request stays accepted forever.
                # A `request_decide`-only admin must not learn anything
                # about the request (its content, or even whether it
                # exists) through the prefill either -- refuse before
                # the lookup, not after.
                flash_error(
                    gettext(
                        'You are not allowed to create a tournament '
                        'from a request.'
                    )
                )
            else:
                source_request = _find_request_for_party(
                    from_request_raw, party.id
                )
                if source_request is not None:
                    if _is_request_recreatable(source_request):
                        _prefill_form_from_request(form, source_request)
                    else:
                        # The request moved on (decided by another admin,
                        # withdrawn) since whatever link led here, or it
                        # is `tournament_created` with a still-live link.
                        # Render the empty form rather than prefill from a
                        # request `create`'s own re-check would refuse.
                        flash_error(
                            gettext(
                                'Request is no longer in the expected state.'
                            )
                        )
                else:
                    # Malformed, unknown, or belongs to another party.
                    flash_error(
                        gettext(
                            'Request is no longer in the expected state.'
                        )
                    )

    form.set_contestant_type_choices()
    form.set_game_format_choices()
    form.set_elimination_mode_choices()
    form.set_score_ordering_choices()

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
    form.set_game_format_choices()
    form.set_elimination_mode_choices()
    form.set_score_ordering_choices()

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
        game_format = (
            GameFormat[form.game_format.data]
            if form.game_format.data
            else None
        )
    except KeyError:
        flash_error(gettext('Invalid game format selected.'))
        return create_form(party.id, form)

    try:
        elimination_mode = (
            EliminationMode[form.elimination_mode.data]
            if form.elimination_mode.data
            else None
        )
    except KeyError:
        flash_error(gettext('Invalid elimination mode selected.'))
        return create_form(party.id, form)

    # Validate game_format + elimination_mode combination.
    if game_format and elimination_mode:
        if not is_valid_combination(game_format, elimination_mode):
            flash_error(
                gettext(
                    'Invalid combination of game format and elimination mode.'
                )
            )
            return create_form(party.id, form)

    try:
        score_ordering = (
            ScoreOrdering[form.score_ordering.data]
            if form.score_ordering.data
            else None
        )
    except KeyError:
        flash_error(gettext('Invalid score ordering selected.'))
        return create_form(party.id, form)
    min_players = form.min_players.data
    max_players = form.max_players.data
    min_teams = form.min_teams.data
    max_teams = form.max_teams.data
    min_players_in_team = form.min_players_in_team.data
    max_players_in_team = form.max_players_in_team.data

    # Clear constraints irrelevant to the selected contestant type.
    if contestant_type == ContestantType.SOLO:
        min_teams = None
        max_teams = None
        min_players_in_team = None
        max_players_in_team = None
    elif contestant_type == ContestantType.TEAM:
        min_players = None
        max_players = None

    # Clear score ordering for non-highscore game formats.
    if game_format != GameFormat.HIGHSCORE:
        score_ordering = None

    # Parse FFA fields.
    point_table = None
    advancement_count = None
    group_size_min = None
    group_size_max = None
    points_carry_to_losers = None
    if game_format == GameFormat.FREE_FOR_ALL:
        point_table_raw = form.point_table.data
        if point_table_raw:
            try:
                point_table = [
                    int(v.strip())
                    for v in point_table_raw.split(',')
                    if v.strip()
                ]
            except ValueError:
                flash_error(
                    gettext(
                        'Point table must be comma-separated integers.'
                    )
                )
                return create_form(party.id, form)
        advancement_count = form.advancement_count.data
        group_size_min = form.group_size_min.data
        group_size_max = form.group_size_max.data
        if (
            elimination_mode == EliminationMode.DOUBLE_ELIMINATION
        ):
            points_carry_to_losers = form.points_carry_to_losers.data

    # `from_request_id` arrives from a hidden form field -- client
    # supplied, so re-load and re-verify party ownership rather than
    # trusting it outright.
    source_request = None
    created_from_request_id = None
    if form.from_request_id.data:
        if not (
            g.user.has_permission('lan_tournament.request_decide')
            and g.user.has_permission('lan_tournament.request_view')
        ):
            # Consuming a request here moves it to `tournament_created`
            # and appoints its proposer as orga -- that is a decision,
            # not a view, so `create`-only permission must not reach
            # it even with a forged hidden field. `request_view` is
            # required too, in lockstep with `create_form`'s own
            # prefill gate above: without it, a `request_decide`-only
            # admin could convert a request blind here and then hit a
            # 403 on `view_request`'s own gate when the redirect (or
            # the appoint-orga flash) tried to point back at it.
            flash_error(
                gettext(
                    'You are not allowed to create a tournament from a request.'
                )
            )
            _clear_stale_request_link(form)
            return create_form(party.id, form)

        source_request = _find_request_for_party(
            form.from_request_id.data, party.id
        )
        if source_request is None:
            # Malformed, unknown, or belongs to another party -- never
            # silently fall through to an unlinked create; the admin
            # asked to create *from* a request.
            flash_error(
                gettext('Request is no longer in the expected state.')
            )
            _clear_stale_request_link(form)
            return create_form(party.id, form)

        if not _is_request_recreatable(source_request):
            # The request's status can have moved on (decided by
            # another admin, withdrawn) between opening this form and
            # submitting it, or it is `tournament_created` with a
            # still-live link -- that link must never be overwritten.
            # This is a UX fast path only: `create_tournament` re-runs
            # the same check under a row lock before it ever commits,
            # which is what actually prevents an orphan tournament for
            # a request that can never reach `tournament_created`.
            flash_error(
                gettext('Request is no longer in the expected state.')
            )
            _clear_stale_request_link(form)
            return create_form(party.id, form)
        created_from_request_id = source_request.id

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
        game_format=game_format,
        elimination_mode=elimination_mode,
        score_ordering=score_ordering,
        point_table=point_table,
        advancement_count=advancement_count,
        group_size_min=group_size_min,
        group_size_max=group_size_max,
        points_carry_to_losers=points_carry_to_losers,
        created_from_request_id=created_from_request_id,
        initiator_id=g.user.id,
    )
    if result.is_err():
        error_message = result.unwrap_err()
        flash_error(gettext(error_message))
        if created_from_request_id is not None and error_message in (
            'Request is no longer in the expected state.',
            'A tournament has already been created from this request.',
        ):
            # The row-locked re-check inside create_tournament caught
            # what the UX fast path above missed (a race, or the
            # unique constraint on created_from_request_id) -- the
            # link is just as dead as if _is_request_recreatable had
            # caught it, so clear it the same way.
            _clear_stale_request_link(form)
        return create_form(party.id, form)

    tournament, _event = result.unwrap()

    if source_request is not None:
        # The request is already linked at this point -- create_tournament
        # committed both in one transaction. This is best-effort only:
        # a failure never rolls back the tournament that already exists.
        orga_result = tournament_request_service.appoint_proposer_orga(
            tournament.id, source_request.proposer_id, g.user.id
        )
        if orga_result.is_err():
            flash_notice(gettext(orga_result.unwrap_err()))

    flash_success(
        gettext(
            'Tournament "%(name)s" has been created.',
            name=tournament.name,
        )
    )

    return redirect_to('.view', tournament_id=tournament.id)


# --- Tournament requests (party-wide admin queue + detail) ---

_PENDING_REQUEST_STATUSES = frozenset(
    {TournamentRequestStatus.submitted, TournamentRequestStatus.accepted}
)


def _build_elimination_mode_labels() -> dict:
    """Resolve `_REQUEST_ELIMINATION_MODE_LABELS` to plain strings.

    Must run inside a request (each `lazy_gettext` label only resolves
    to text, in the current locale, once `str()`'d), so this builds
    the dict per-view-call rather than once at import time.
    """
    return {
        mode: str(label)
        for mode, label in _REQUEST_ELIMINATION_MODE_LABELS.items()
    }


@blueprint.app_template_global('lan_tournament_pending_request_count')
def _pending_request_count_for_nav(party_id) -> int:
    """Return the party's open-request count for the admin nav tab badge.

    Registered as a Jinja global rather than threaded through every
    view's template context: the layout template's `before_body`
    block, shared by every page in this blueprint, is where the tab
    label is built (compromise C1 -- see guardrails), and most of
    those views and templates belong to other issues.

    Viewers without `request_view` never see the requests tab this
    count feeds, so skip the query for them entirely rather than
    loading every request row on every lan_tournament admin page.
    """
    if not g.user.has_permission('lan_tournament.request_view'):
        return 0

    return (
        tournament_request_repository.count_requests_for_party_with_statuses(
            PartyID(party_id), _PENDING_REQUEST_STATUSES
        )
    )


@blueprint.get('/for_party/<party_id>/requests')
@permission_required('lan_tournament.request_view')
@templated
def requests_for_party(party_id):
    """List tournament requests for that party."""
    party = _get_party_or_404(party_id)

    all_requests = tournament_request_service.get_visible_requests_for_user(
        party.id, g.user.id, is_admin=True
    )

    status_counts = Counter(r.status.value for r in all_requests)

    status_filter = None
    status_filter_raw = request.args.get('status')
    if status_filter_raw:
        try:
            status_filter = TournamentRequestStatus(status_filter_raw)
        except ValueError:
            status_filter = None

    visible_requests = (
        [r for r in all_requests if r.status is status_filter]
        if status_filter is not None
        else all_requests
    )

    pending_requests = [
        r for r in visible_requests if r.status in _PENDING_REQUEST_STATUSES
    ]
    done_requests = [
        r
        for r in visible_requests
        if r.status not in _PENDING_REQUEST_STATUSES
    ]

    now = datetime.now(UTC)
    stale_request_ids = {
        r.id
        for r in pending_requests
        if tournament_request_domain_service.is_stale_accepted(r, now)
    }

    gaps_by_request_id = {
        r.id: tournament_request_domain_service.analyze_field_gap(r)
        for r in visible_requests
    }

    users_by_id = user_service.get_users_indexed_by_id(
        {r.proposer_id for r in all_requests}
    )

    return {
        'party': party,
        'pending_requests': pending_requests,
        'done_requests': done_requests,
        'stale_request_ids': stale_request_ids,
        'gaps_by_request_id': gaps_by_request_id,
        'users_by_id': users_by_id,
        'status_counts': status_counts,
        'status_filter': status_filter,
        'total_count': len(all_requests),
        'elimination_mode_labels': _build_elimination_mode_labels(),
    }


@blueprint.get('/requests/<request_id>')
@permission_required('lan_tournament.request_view')
@templated
def view_request(request_id, erroneous_reject_form=None):
    """Show a tournament request's detail, with the create-gap preview."""
    tournament_request = _get_request_or_404(request_id)
    party = party_service.get_party(tournament_request.party_id)

    gap = tournament_request_domain_service.analyze_field_gap(
        tournament_request
    )
    history = tournament_request_service.get_request_history(
        tournament_request.id
    )

    user_ids = {tournament_request.proposer_id}
    if tournament_request.decided_by_id:
        user_ids.add(tournament_request.decided_by_id)
    user_ids.update(
        entry.initiator_id for entry in history if entry.initiator_id
    )
    users_by_id = user_service.get_users_indexed_by_id(user_ids)

    reject_form = (
        erroneous_reject_form
        if erroneous_reject_form is not None
        else TournamentRequestRejectForm()
    )

    return {
        'party': party,
        'tournament_request': tournament_request,
        'gap': gap,
        'history': history,
        'users_by_id': users_by_id,
        'reject_form': reject_form,
        'elimination_mode_labels': _build_elimination_mode_labels(),
    }


@blueprint.post('/requests/<request_id>/accept')
@permission_required('lan_tournament.request_decide')
def accept_request(request_id):
    """Accept a tournament request.

    Checked upfront, before the service call: both success and error
    paths below redirect to `.view_request`/`.requests_for_party`,
    which themselves require `request_view` -- without this check, a
    decide-only admin (who lacks `request_view`) would commit the
    decision, then get a 403 from their own redirect.
    """
    if not g.user.has_permission('lan_tournament.request_view'):
        abort(403)

    tournament_request = _get_request_or_404(request_id)

    match tournament_request_service.accept_request(
        tournament_request.id, g.user.id
    ):
        case Ok(_):
            flash_success(gettext('Tournament request has been accepted.'))
        case Err(error_message):
            flash_error(gettext(error_message))

    return redirect_to('.view_request', request_id=tournament_request.id)


@blueprint.post('/requests/<request_id>/reject')
@permission_required('lan_tournament.request_decide')
def reject_request(request_id):
    """Reject a tournament request.

    Checked upfront, before the service call: both the validation-error
    re-render (`view_request`, whose own decorator re-checks
    `request_view`) and the success/error redirects need `request_view`
    -- without this check, a decide-only admin (who lacks
    `request_view`) would commit the decision, then get a 403 either
    from the over-long-reason re-render or from their own redirect.
    """
    if not g.user.has_permission('lan_tournament.request_view'):
        abort(403)

    tournament_request = _get_request_or_404(request_id)

    form = TournamentRequestRejectForm(request.form)
    if not form.validate():
        # `InputRequired` only rejects a wholly empty field -- a
        # whitespace-only reason is truthy and slips past it, so an
        # empty-after-strip reason is the one case with nothing worth
        # preserving (mirroring the service's own `reason.strip()`
        # check, and its precedence over every other form validator).
        if not (form.reason.data or '').strip():
            flash_error(gettext('A reason is required to reject a request.'))
            return redirect_to(
                '.view_request', request_id=tournament_request.id
            )

        # Any other validator failure (too long, control characters,
        # or any future one) keeps the typed text -- flash the form's
        # own message for the field instead of assuming it was the
        # length check, and re-render with the bound (erroneous) form.
        error_message = (
            form.reason.errors[0]
            if form.reason.errors
            else 'The reason must not exceed 2000 characters.'
        )
        flash_error(gettext(error_message))
        return view_request(request_id, erroneous_reject_form=form)

    reason = form.reason.data.strip()

    match tournament_request_service.reject_request(
        tournament_request.id, g.user.id, reason
    ):
        case Ok(_):
            flash_success(gettext('Tournament request has been rejected.'))
            return redirect_to(
                '.requests_for_party', party_id=tournament_request.party_id
            )
        case Err(error_message):
            flash_error(gettext(error_message))
            if error_message in (
                'The reason must not contain control characters.',
                'The reason must not exceed 2000 characters.',
            ):
                # The form itself didn't catch this (e.g. the control-
                # character check isn't wired into the form yet) -- the
                # service did, so preserve the typed text the same way
                # the form-validation branch above does, instead of
                # redirecting it into oblivion.
                return view_request(request_id, erroneous_reject_form=form)
            return redirect_to(
                '.requests_for_party', party_id=tournament_request.party_id
            )


def _clear_stale_request_link(form) -> None:
    """Clear the hidden `from_request_id` and explain why.

    Without this, a re-render after a request-link refusal keeps the
    same hidden field, so every resubmit of that form is refused
    again for the same reason -- the admin has no way to proceed
    except abandoning the form and starting a fresh, unlinked create.
    """
    form.from_request_id.data = ''
    flash_notice(
        gettext(
            'This form is no longer linked to a tournament request. '
            'Submitting it again creates a tournament without a '
            'request link.'
        )
    )


def _find_request_for_party(raw_request_id, party_id):
    """Look up a tournament request and verify it belongs to that party.

    `raw_request_id` arrives from a query string or a hidden form
    field -- client supplied, so treat it as untrusted: return
    ``None`` on anything that doesn't parse to a UUID, doesn't exist,
    or belongs to a different party. There is no silent cross-party
    fallback.
    """
    try:
        parsed_id = TournamentRequestID(UUID(str(raw_request_id)))
    except ValueError:
        return None

    tournament_request = tournament_request_repository.find_request(
        parsed_id
    )
    if tournament_request is None or tournament_request.party_id != party_id:
        return None

    return tournament_request


def _is_request_recreatable(tournament_request) -> bool:
    """Return whether a create form may be filled/submitted from this request.

    Mirrors `tournament_request_service._is_linkable`'s status check:
    either `accepted` (linking for the first time) or
    `tournament_created` with `tournament_deleted` (re-linking after
    the prior tournament was deleted). This is a UX fast path only --
    `tournament_service.create_tournament` re-runs the equivalent
    check under a row lock, which is what actually enforces it.
    """
    return (
        tournament_request.status is TournamentRequestStatus.accepted
        or tournament_request.tournament_deleted
    )


def _prefill_form_from_request(form, tournament_request):
    """Prefill the create-tournament form from an accepted request.

    Only fields with an unambiguous `create_tournament` counterpart are
    copied. `team_size`/`participant_limit` are deliberately left out:
    they depend on a contestant type (solo vs. team) the request never
    supplies, so the admin maps them by hand -- see `analyze_field_gap`.
    """
    form.from_request_id.data = str(tournament_request.id)
    form.name.data = tournament_request.name
    form.game.data = tournament_request.game
    form.description.data = tournament_request.description
    form.ruleset.data = tournament_request.special_rules
    form.game_format.data = tournament_request.game_format.name
    form.elimination_mode.data = tournament_request.elimination_mode.name
    form.start_time.data = to_user_timezone(
        tournament_request.preferred_start_time
    )


def _get_request_or_404(request_id) -> TournamentRequest:
    # Same parse-before-query reason as _get_tournament_or_404 above.
    try:
        request_id = TournamentRequestID(UUID(str(request_id)))
    except ValueError:
        abort(404)

    tournament_request = tournament_request_repository.find_request(
        request_id
    )

    if tournament_request is None:
        abort(404)

    return tournament_request


@blueprint.get('/requests/<request_id>/update')
@permission_required('lan_tournament.request_decide')
@templated
def update_request_form(request_id, erroneous_form=None):
    """Show the admin form to edit an open tournament request."""
    if not g.user.has_permission('lan_tournament.request_view'):
        abort(403)

    tournament_request = _get_request_or_404(request_id)

    if not tournament_request.is_editable:
        flash_error(gettext('This request can no longer be edited.'))
        return redirect_to(
            '.view_request', request_id=tournament_request.id
        )

    party = party_service.get_party(tournament_request.party_id)

    if erroneous_form is not None:
        form = erroneous_form
    else:
        form = TournamentRequestUpdateForm(
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
        'party': party,
        'tournament_request': tournament_request,
        'form': form,
        'party_capacity': party.max_ticket_quantity,
    }


@blueprint.post('/requests/<request_id>/update')
@permission_required('lan_tournament.request_decide')
def update_request(request_id):
    """Update an open tournament request (admin edit).

    Refuses upfront, by redirect, once `tournament_request.is_editable`
    is false (any status but `submitted`) -- `update_request`'s own
    `expected_status`-narrowed precondition (still effectively
    `submitted`) stays as the row-locked defense-in-depth for the
    race between this check and the service's own re-read, not the
    primary guard.
    """
    if not g.user.has_permission('lan_tournament.request_view'):
        abort(403)

    tournament_request = _get_request_or_404(request_id)

    if not tournament_request.is_editable:
        flash_error(gettext('This request can no longer be edited.'))
        return redirect_to(
            '.view_request', request_id=tournament_request.id
        )

    party = party_service.get_party(tournament_request.party_id)

    form = TournamentRequestUpdateForm(request.form)
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
        tournament_request.id,
        g.user.id,
        by='admin',
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
            return redirect_to(
                '.view_request', request_id=tournament_request.id
            )
        case Err(error_message):
            flash_error(gettext(error_message))
            return update_request_form(request_id, form)


@blueprint.get('/tournaments/<tournament_id>/update')
@permission_required('lan_tournament.update')
@templated
def update_form(tournament_id, erroneous_form=None):
    """Show form to update the tournament."""
    tournament = _get_tournament_or_404(tournament_id)

    party = party_service.get_party(tournament.party_id)

    is_locked = tournament.tournament_status in EDIT_LOCKED_STATUSES

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
        if tournament.game_format is not None:
            data['game_format'] = tournament.game_format.name
        else:
            data['game_format'] = ''
        if tournament.elimination_mode is not None:
            data['elimination_mode'] = tournament.elimination_mode.name
        else:
            data['elimination_mode'] = ''
        if tournament.score_ordering is not None:
            data['score_ordering'] = tournament.score_ordering.name
        else:
            data['score_ordering'] = ''
        # Serialize point_table list back to comma-separated string.
        if tournament.point_table is not None:
            data['point_table'] = ', '.join(
                str(v) for v in tournament.point_table
            )
        else:
            data['point_table'] = ''
        # Pre-populate points_carry_to_losers checkbox.
        data['points_carry_to_losers'] = (
            tournament.points_carry_to_losers or False
        )
        form = TournamentUpdateForm(data=data)

    form.set_contestant_type_choices()
    form.set_game_format_choices()
    form.set_elimination_mode_choices()
    form.set_score_ordering_choices()

    return {
        'party': party,
        'tournament': tournament,
        'form': form,
        'is_locked': is_locked,
    }


@blueprint.post('/tournaments/<tournament_id>')
@permission_required('lan_tournament.update')
def update(tournament_id):
    """Update the tournament."""
    tournament = _get_tournament_or_404(tournament_id)

    is_locked = tournament.tournament_status in EDIT_LOCKED_STATUSES

    # Disabled fields are not submitted by browsers.  Inject the
    # stored values so WTForms validation passes normally.
    if is_locked:
        formdata = request.form.copy()
        formdata['name'] = tournament.name
        formdata['game'] = tournament.game or ''
        formdata['contestant_type'] = (
            tournament.contestant_type.name
            if tournament.contestant_type
            else ''
        )
        formdata['game_format'] = (
            tournament.game_format.name
            if tournament.game_format
            else ''
        )
        formdata['elimination_mode'] = (
            tournament.elimination_mode.name
            if tournament.elimination_mode
            else ''
        )
        formdata['score_ordering'] = (
            tournament.score_ordering.name
            if tournament.score_ordering
            else ''
        )
        if tournament.start_time:
            formdata['start_time'] = to_user_timezone(
                tournament.start_time
            ).strftime('%Y-%m-%dT%H:%M')
        else:
            formdata['start_time'] = ''
        formdata['min_players'] = (
            str(tournament.min_players)
            if tournament.min_players is not None
            else ''
        )
        formdata['max_players'] = (
            str(tournament.max_players)
            if tournament.max_players is not None
            else ''
        )
        formdata['min_teams'] = (
            str(tournament.min_teams)
            if tournament.min_teams is not None
            else ''
        )
        formdata['max_teams'] = (
            str(tournament.max_teams)
            if tournament.max_teams is not None
            else ''
        )
        formdata['min_players_in_team'] = (
            str(tournament.min_players_in_team)
            if tournament.min_players_in_team is not None
            else ''
        )
        formdata['max_players_in_team'] = (
            str(tournament.max_players_in_team)
            if tournament.max_players_in_team is not None
            else ''
        )
        # Inject locked FFA fields.
        if tournament.point_table is not None:
            formdata['point_table'] = ', '.join(
                str(v) for v in tournament.point_table
            )
        else:
            formdata['point_table'] = ''
        formdata['group_size_min'] = (
            str(tournament.group_size_min)
            if tournament.group_size_min is not None
            else ''
        )
        formdata['group_size_max'] = (
            str(tournament.group_size_max)
            if tournament.group_size_max is not None
            else ''
        )
        formdata['advancement_count'] = (
            str(tournament.advancement_count)
            if tournament.advancement_count is not None
            else ''
        )
        if tournament.points_carry_to_losers:
            formdata['points_carry_to_losers'] = 'y'
    else:
        formdata = request.form

    form = TournamentUpdateForm(formdata)
    form.set_contestant_type_choices()
    form.set_game_format_choices()
    form.set_elimination_mode_choices()
    form.set_score_ordering_choices()

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
        game_format = (
            GameFormat[form.game_format.data]
            if form.game_format.data
            else None
        )
    except KeyError:
        flash_error(gettext('Invalid game format selected.'))
        return update_form(tournament.id, form)

    try:
        elimination_mode = (
            EliminationMode[form.elimination_mode.data]
            if form.elimination_mode.data
            else None
        )
    except KeyError:
        flash_error(gettext('Invalid elimination mode selected.'))
        return update_form(tournament.id, form)

    # Validate game_format + elimination_mode combination.
    if game_format and elimination_mode:
        if not is_valid_combination(game_format, elimination_mode):
            flash_error(
                gettext(
                    'Invalid combination of game format and elimination mode.'
                )
            )
            return update_form(tournament.id, form)

    try:
        score_ordering = (
            ScoreOrdering[form.score_ordering.data]
            if form.score_ordering.data
            else None
        )
    except KeyError:
        flash_error(gettext('Invalid score ordering selected.'))
        return update_form(tournament.id, form)
    min_players = form.min_players.data
    max_players = form.max_players.data
    min_teams = form.min_teams.data
    max_teams = form.max_teams.data
    min_players_in_team = form.min_players_in_team.data
    max_players_in_team = form.max_players_in_team.data

    # Clear constraints irrelevant to the selected contestant type.
    if contestant_type == ContestantType.SOLO:
        min_teams = None
        max_teams = None
        min_players_in_team = None
        max_players_in_team = None
    elif contestant_type == ContestantType.TEAM:
        min_players = None
        max_players = None

    # Clear score ordering for non-highscore game formats.
    if game_format != GameFormat.HIGHSCORE:
        score_ordering = None

    # Parse FFA fields.
    point_table = None
    advancement_count = None
    group_size_min = None
    group_size_max = None
    points_carry_to_losers = None
    if game_format == GameFormat.FREE_FOR_ALL:
        point_table_raw = form.point_table.data
        if point_table_raw:
            try:
                point_table = [
                    int(v.strip())
                    for v in point_table_raw.split(',')
                    if v.strip()
                ]
            except ValueError:
                flash_error(
                    gettext(
                        'Point table must be comma-separated integers.'
                    )
                )
                return update_form(tournament.id, form)
        advancement_count = form.advancement_count.data
        group_size_min = form.group_size_min.data
        group_size_max = form.group_size_max.data
        if (
            elimination_mode == EliminationMode.DOUBLE_ELIMINATION
        ):
            points_carry_to_losers = form.points_carry_to_losers.data

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
        game_format=game_format,
        elimination_mode=elimination_mode,
        score_ordering=score_ordering,
        point_table=point_table,
        advancement_count=advancement_count,
        group_size_min=group_size_min,
        group_size_max=group_size_max,
        points_carry_to_losers=points_carry_to_losers,
    )
    if result.is_err():
        flash_error(gettext(result.unwrap_err()))
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

    tournament_service.delete_tournament(tournament.id, g.user.id)

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


# The only way back out of COMPLETED. Reserved for global admins:
# the site blueprint's orga status actions refuse a completed
# tournament, so an orga who completes one prematurely needs an
# admin to undo it. change_status() clears the recorded winner.
@blueprint.post('/tournaments/<tournament_id>/reopen')
@permission_required('lan_tournament.administrate')
def reopen(tournament_id):
    """Reopen a completed tournament."""
    return _change_status(tournament_id, TournamentStatus.ONGOING)


def _change_status(tournament_id, new_status: TournamentStatus):
    """Change the tournament status."""
    tournament = _get_tournament_or_404(tournament_id)

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


@blueprint.post('/tournaments/<tournament_id>/generate_bracket')
@permission_required('lan_tournament.administrate')
def generate_bracket(tournament_id):
    """Generate bracket for the tournament."""
    tournament = _get_tournament_or_404(tournament_id)

    allowed_statuses = (
        TournamentStatus.REGISTRATION_CLOSED,
        TournamentStatus.ONGOING,
    )
    if tournament.tournament_status not in allowed_statuses:
        flash_error(
            gettext(
                'Bracket can only be generated when registration '
                'is closed or tournament is ongoing.'
            )
        )
        return redirect_to('.view', tournament_id=tournament.id)

    # Check for force regenerate parameter
    force_regenerate = request.form.get('force', 'false').lower() == 'true'

    result: Ok[int] | Err[str] | None = None
    match (tournament.game_format, tournament.elimination_mode):
        case (GameFormat.ONE_V_ONE, EliminationMode.SINGLE_ELIMINATION):
            result = (
                tournament_match_service.generate_single_elimination_bracket(
                    tournament.id,
                    force_regenerate=force_regenerate,
                    initiator_id=g.user.id,
                )
            )
        case (GameFormat.ONE_V_ONE, EliminationMode.DOUBLE_ELIMINATION):
            result = (
                tournament_match_service.generate_double_elimination_bracket(
                    tournament.id,
                    force_regenerate=force_regenerate,
                    initiator_id=g.user.id,
                )
            )
        case (GameFormat.ONE_V_ONE, EliminationMode.ROUND_ROBIN):
            result = tournament_match_service.generate_round_robin_bracket(
                tournament.id,
                force_regenerate=force_regenerate,
                initiator_id=g.user.id,
            )
        case (GameFormat.FREE_FOR_ALL, _):
            flash_error(
                gettext(
                    'Free-for-All tournaments generate rounds on demand.'
                )
            )
            return redirect_to('.view', tournament_id=tournament.id)
        case (GameFormat.HIGHSCORE, EliminationMode.NONE):
            flash_error(gettext('Highscore tournaments do not use brackets.'))
            return redirect_to('.view', tournament_id=tournament.id)
        case _:
            flash_error(gettext('Unknown game format or elimination mode.'))
            return redirect_to('.view', tournament_id=tournament.id)

    if result is None:
        return redirect_to('.view', tournament_id=tournament.id)

    match result:
        case Ok(match_count):
            flash_success(
                gettext(
                    'Bracket generated with %(count)d matches.',
                    count=match_count,
                )
            )
        case Err(error_message):
            flash_error(
                gettext(
                    'Bracket generation failed: %(error)s',
                    error=error_message,
                )
            )

    return redirect_to('.view', tournament_id=tournament.id)


@blueprint.post('/for_party/<party_id>/setup_email_templates')
@permission_required('lan_tournament.administrate')
def setup_email_templates_for_party(party_id):
    """Create default tournament notification email snippets (match-ready
    and request accept/reject) for the party's brand.
    """
    party = _get_party_or_404(party_id)
    brand = brand_service.get_brand(party.brand_id)
    current_user = g.user

    created_match_ready = (
        tournament_notification_service.create_match_ready_email_snippets(
            brand, current_user,
        )
    )
    created_request = (
        tournament_notification_service
        .create_tournament_request_email_snippets(brand, current_user)
    )
    if created_match_ready or created_request:
        flash_success(
            gettext('Missing tournament notification email templates created.')
        )
    else:
        flash_notice(
            gettext('Tournament notification email templates already exist.')
        )

    return redirect_to('.overview', party_id=party.id)


# -------------------------------------------------------------------- #
# teams


@blueprint.get('/tournaments/<tournament_id>/teams')
@permission_required('lan_tournament.view')
@templated
def teams_for_tournament(tournament_id):
    """List teams for that tournament."""
    tournament = _get_tournament_or_404(tournament_id)

    if tournament.contestant_type != ContestantType.TEAM:
        abort(404)

    party = party_service.get_party(tournament.party_id)

    teams = tournament_team_service.get_teams_for_tournament(tournament.id)
    member_counts = tournament_team_service.get_team_member_counts(
        tournament.id
    )

    return {
        'party': party,
        'tournament': tournament,
        'teams': teams,
        'member_counts': member_counts,
    }


@blueprint.get('/teams/<team_id>')
@permission_required('lan_tournament.view')
@templated
def view_team(team_id):
    """Show team detail with member management."""
    team = _get_team_or_404(team_id)
    tournament = _get_tournament_or_404(team.tournament_id)
    party = party_service.get_party(tournament.party_id)

    members, users_by_id = _get_team_members(team.id)

    # Build seat lookup
    user_ids = {m.user_id for m in members}
    seats_by_user_id = build_seat_lookup(user_ids, party.id)

    # Build transfer captain form
    transfer_form = TransferCaptainForm()
    transfer_form.new_captain.choices = _build_transfer_captain_choices(
        team, members, users_by_id
    )

    return {
        'party': party,
        'tournament': tournament,
        'team': team,
        'members': members,
        'users_by_id': users_by_id,
        'seats_by_user_id': seats_by_user_id,
        'transfer_form': transfer_form,
        'add_member_form': AddTeamMemberForm(),
    }


@blueprint.post('/teams/<team_id>/transfer_captain')
@permission_required('lan_tournament.administrate')
def transfer_captain(team_id):
    """Transfer captain role to another team member."""
    team = _get_team_or_404(team_id)

    form = TransferCaptainForm(request.form)

    # Build choices from team members (excluding current captain)
    members, users_by_id = _get_team_members(team.id)
    form.new_captain.choices = _build_transfer_captain_choices(
        team, members, users_by_id
    )

    if not form.validate():
        flash_error(gettext('Transfer failed.'))
        return redirect_to('.view_team', team_id=team.id)

    new_captain_user_id = UserID(UUID(form.new_captain.data))

    result = tournament_team_service.transfer_captain(
        team.id, new_captain_user_id
    )
    match result:
        case Ok(_):
            new_captain = users_by_id.get(new_captain_user_id)
            name = new_captain.screen_name if new_captain else '?'
            flash_success(
                gettext(
                    'Captain transferred to %(name)s.',
                    name=name,
                )
            )
        case Err(error):
            flash_error(gettext(error))

    return redirect_to('.view_team', team_id=team.id)


@blueprint.post('/teams/<team_id>/add_member')
@permission_required('lan_tournament.administrate')
def admin_add_team_member(team_id):
    """Admin: add a participant to a team."""
    team = _get_team_or_404(team_id)

    form = AddTeamMemberForm(request.form)
    if not form.validate():
        flash_error(gettext('Could not add member.'))
        return redirect_to('.view_team', team_id=team.id)

    user = form.user

    result = tournament_team_service.admin_add_member(team.id, user.id)
    match result:
        case Ok(_):
            flash_success(
                gettext(
                    '%(name)s added to the team.',
                    name=user.screen_name,
                )
            )
        case Err(error):
            flash_error(gettext(error))

    return redirect_to('.view_team', team_id=team.id)


@blueprint.post('/teams/<team_id>/remove_member/<user_id>')
@permission_required('lan_tournament.administrate')
def admin_remove_team_member(team_id, user_id):
    """Admin: remove a member from a team."""
    team = _get_team_or_404(team_id)

    try:
        member_user_id = UserID(UUID(user_id))
    except ValueError:
        abort(404)

    result = tournament_team_service.remove_team_member(
        team.id, member_user_id
    )
    match result:
        case Ok(_):
            flash_success(gettext('Member removed from the team.'))
        case Err(error):
            flash_error(gettext(error))

    return redirect_to('.view_team', team_id=team.id)


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

    captain_user_id = form.captain_user.id

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
        flash_error(gettext(result.unwrap_err()))
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


# -------------------------------------------------------------------- #
# participants


@blueprint.get('/tournaments/<tournament_id>/participants')
@permission_required('lan_tournament.view')
@templated
def participants_for_tournament(tournament_id):
    """List participants for that tournament."""
    tournament = _get_tournament_or_404(tournament_id)
    party = party_service.get_party(tournament.party_id)

    participants = (
        tournament_participant_service.get_participants_for_tournament(
            tournament.id
        )
    )

    # Fetch ticket status via service layer, reusing already-fetched
    # participants to avoid a redundant DB query.
    users_with_tickets, participants_without_tickets = (
        tournament_participant_service.get_ticket_status_for_participants(
            tournament.id,
            party.id,
            participants=participants,
        )
    )

    is_team_tournament = tournament.contestant_type == ContestantType.TEAM

    # Resolve user names
    user_ids = {p.user_id for p in participants}
    users_by_id = user_service.get_users_indexed_by_id(user_ids)

    # Resolve team names
    teams_by_id = {}
    if is_team_tournament:
        teams = tournament_team_service.get_teams_for_tournament(tournament.id)
        teams_by_id = {t.id: t for t in teams}

    # Build seat lookup
    all_user_ids = set(users_by_id.keys())
    seats_by_user_id = build_seat_lookup(all_user_ids, party.id)

    # Build team members lookup (only for team tournaments)
    team_members_by_team_id: dict[
        TournamentTeamID, list[tuple[str, str | None]]
    ] = {}
    if tournament.contestant_type == ContestantType.TEAM:
        team_members_by_team_id = build_team_members_lookup(
            participants,
            set(teams_by_id.keys()),
            users_by_id,
            seats_by_user_id,
        )

    return {
        'party': party,
        'tournament': tournament,
        'participants': participants,
        'users_by_id': users_by_id,
        'teams_by_id': teams_by_id,
        'users_with_tickets': users_with_tickets,
        'participants_without_tickets': participants_without_tickets,
        'is_team_tournament': is_team_tournament,
        'seats_by_user_id': seats_by_user_id,
        'team_members_by_team_id': team_members_by_team_id,
    }


@blueprint.get('/tournaments/<tournament_id>/participants/add')
@permission_required('lan_tournament.administrate')
@templated
def add_participant_form(tournament_id, erroneous_form=None):
    """Show form to add a participant."""
    tournament = _get_tournament_or_404(tournament_id)
    party = party_service.get_party(tournament.party_id)

    form = erroneous_form if erroneous_form else AddParticipantForm()

    return {
        'party': party,
        'tournament': tournament,
        'form': form,
    }


@blueprint.post('/tournaments/<tournament_id>/participants/add')
@permission_required('lan_tournament.administrate')
def add_participant(tournament_id):
    """Add a participant to the tournament."""
    tournament = _get_tournament_or_404(tournament_id)

    form = AddParticipantForm(request.form)

    if not form.validate():
        return add_participant_form(tournament.id, form)

    user = form.user

    match tournament_participant_service.admin_add_participant(
        tournament.id, user.id, initiator=g.user
    ):
        case Ok((_, _event)):
            flash_success(
                gettext(
                    'Participant "%(name)s" has been added.',
                    name=user.screen_name,
                )
            )
            return redirect_to(
                '.participants_for_tournament',
                tournament_id=tournament.id,
            )
        case Err(error_message):
            flash_error(
                gettext(
                    'Could not add participant: %(error)s',
                    error=error_message,
                )
            )
            return add_participant_form(tournament.id, form)


@blueprint.post(
    '/tournaments/<tournament_id>/participants/<participant_id>/remove'
)
@permission_required('lan_tournament.administrate')
def remove_participant(tournament_id, participant_id):
    """Remove a participant from the tournament."""
    tournament = _get_tournament_or_404(tournament_id)

    result = tournament_participant_service.admin_remove_participant(
        tournament.id, participant_id, initiator=g.user
    )

    match result:
        case Ok(_):
            flash_success(gettext('Participant has been removed.'))
        case Err(error_message):
            flash_error(
                gettext(
                    'Could not remove participant: %(error)s',
                    error=error_message,
                )
            )

    return redirect_to(
        '.participants_for_tournament',
        tournament_id=tournament.id,
    )


@blueprint.post(
    '/tournaments/<tournament_id>/participants/remove_without_tickets'
)
@permission_required('lan_tournament.administrate')
def remove_participants_without_tickets(tournament_id):
    """Remove all participants who don't have valid tickets."""
    tournament = _get_tournament_or_404(tournament_id)
    party = party_service.get_party(tournament.party_id)

    result = tournament_participant_service.remove_participants_without_tickets(
        tournament.id, party.id,
        initiator_id=g.user.id,
    )

    match result:
        case Ok(count):
            if count == 0:
                flash_notice(
                    gettext(
                        'No participants without tickets found.'
                        ' Ticket status may have changed'
                        ' since the page was loaded.'
                    )
                )
            elif tournament.contestant_type == ContestantType.TEAM:
                flash_success(
                    gettext(
                        '%(count)s participant(s) removed. '
                        'Captain roles transferred where '
                        'needed.',
                        count=count,
                    )
                )
            else:
                flash_success(
                    gettext(
                        '%(count)s participant(s) removed.',
                        count=count,
                    )
                )
        case Err(error_message):
            flash_error(
                gettext('Could not remove: %(error)s', error=error_message)
            )

    return redirect_to(
        '.participants_for_tournament', tournament_id=tournament.id
    )


@blueprint.get('/tournaments/<tournament_id>/orgas')
@permission_required('lan_tournament.view')
@templated
def orgas_for_tournament(tournament_id):
    """List orgas for that tournament."""
    tournament = _get_tournament_or_404(tournament_id)
    party = party_service.get_party(tournament.party_id)

    orgas = tournament_orga_service.get_orgas_for_tournament(tournament.id)

    user_ids = {orga.user_id for orga in orgas}
    users_by_id = user_service.get_users_indexed_by_id(user_ids)

    form = TournamentOrgaAssignForm()

    return {
        'party': party,
        'tournament': tournament,
        'orgas': orgas,
        'users_by_id': users_by_id,
        'form': form,
    }


@blueprint.post('/tournaments/<tournament_id>/orgas/assign')
@permission_required('lan_tournament.orga_assign')
def assign_orga(tournament_id):
    """Assign a user as an orga of the tournament."""
    tournament = _get_tournament_or_404(tournament_id)

    form = TournamentOrgaAssignForm(request.form)

    if not form.validate():
        flash_error(gettext('Could not assign orga.'))
        return redirect_to('.orgas_for_tournament', tournament_id=tournament.id)

    screen_name = form.screen_name.data.strip()
    user = user_service.find_user_by_screen_name(screen_name)
    if user is None:
        flash_error(
            gettext(
                'Unknown username "%(screen_name)s".',
                screen_name=screen_name,
            )
        )
        return redirect_to('.orgas_for_tournament', tournament_id=tournament.id)

    # Do not grant tournament rights to an account that must not act.
    if user.deleted or user.suspended:
        flash_error(
            gettext(
                'Cannot assign "%(screen_name)s": the account is '
                'deleted or suspended.',
                screen_name=screen_name,
            )
        )
        return redirect_to('.orgas_for_tournament', tournament_id=tournament.id)

    duties = (form.duties.data or '').strip() or None

    match tournament_orga_service.assign_orga(
        tournament.id, user.id, g.user.id, duties=duties
    ):
        case Ok(_):
            flash_success(
                gettext(
                    '%(name)s has been assigned as orga.',
                    name=user.screen_name,
                )
            )
        case Err(error_message):
            flash_error(
                gettext(
                    'Could not assign orga: %(error)s',
                    error=gettext(error_message),
                )
            )

    return redirect_to('.orgas_for_tournament', tournament_id=tournament.id)


@blueprint.post('/tournaments/<tournament_id>/orgas/<user_id>/revoke')
@permission_required('lan_tournament.orga_assign')
def revoke_orga(tournament_id, user_id):
    """Revoke a user's orga assignment for the tournament."""
    tournament = _get_tournament_or_404(tournament_id)

    try:
        orga_user_id = UserID(UUID(user_id))
    except ValueError:
        abort(404)

    match tournament_orga_service.revoke_orga(
        tournament.id, orga_user_id, g.user.id
    ):
        case Ok(_):
            flash_success(gettext('Orga assignment revoked.'))
        case Err(error_message):
            flash_error(
                gettext(
                    'Could not revoke orga: %(error)s',
                    error=gettext(error_message),
                )
            )

    return redirect_to('.orgas_for_tournament', tournament_id=tournament.id)


def _get_team_members(team_id):
    """Fetch active team members and resolve their user info."""
    members = tournament_team_service.get_team_members(team_id)
    user_ids = {m.user_id for m in members}
    users_by_id = user_service.get_users_indexed_by_id(user_ids)
    return members, users_by_id



def _build_transfer_captain_choices(team, members, users_by_id):
    """Build SelectField choices for captain transfer."""
    non_captain_members = [
        m for m in members if m.user_id != team.captain_user_id
    ]
    return [
        (str(u.id), u.screen_name)
        for m in non_captain_members
        if (u := users_by_id.get(m.user_id)) and u.screen_name
    ]


def _get_party_or_404(party_id) -> Party:
    party = party_service.find_party(party_id)

    if party is None:
        abort(404)

    return party


def _get_tournament_or_404(tournament_id) -> Tournament:
    try:
        tournament_id = TournamentID(UUID(str(tournament_id)))
    except ValueError:
        abort(404)

    tournament = tournament_service.find_tournament(tournament_id)

    if tournament is None:
        abort(404)

    return tournament


def _get_team_or_404(team_id) -> TournamentTeam:
    try:
        team_id = TournamentTeamID(UUID(str(team_id)))
    except ValueError:
        abort(404)

    team = tournament_team_service.find_team(team_id)

    if team is None:
        abort(404)

    return team


def _get_match_or_404(match_id) -> TournamentMatch:
    # Pass a real UUID on; UUID-keyed lookups downstream miss a `str`.
    try:
        match_uuid = TournamentMatchID(UUID(str(match_id)))
    except ValueError:
        abort(404)

    match = tournament_match_service.find_match(match_uuid)
    if match is None:
        abort(404)
    return match


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


@blueprint.get('/tournaments/<tournament_id>/matches')
@permission_required('lan_tournament.view')
@templated
def matches_for_tournament(tournament_id):
    """List matches for that tournament."""
    tournament = _get_tournament_or_404(tournament_id)
    party = party_service.get_party(tournament.party_id)

    only = request.args.get('only', 'open')

    matches = tournament_match_service.get_matches_for_tournament_ordered(
        tournament.id
    )

    # Get contestants for each match
    match_data = []
    all_contestants = []
    for match in matches:
        contestants = tournament_match_service.get_contestants_for_match(
            match.id
        )
        match_data.append(
            {
                'match': match,
                'contestants': contestants,
            }
        )
        all_contestants.append(contestants)

    # Compute counts before filtering.
    total_count = len(match_data)
    ready_count = sum(1 for e in match_data if _is_match_ready(e))
    open_count = sum(1 for e in match_data if _is_match_open(e))
    match_quantities = {
        'all': total_count,
        'open': open_count,
        'ready': ready_count,
    }

    # Apply status filter.
    if only == 'open':
        match_data = [e for e in match_data if _is_match_open(e)]
    elif only == 'ready':
        match_data = [e for e in match_data if _is_match_ready(e)]
    # 'all' → no filtering

    teams_by_id, participants_by_id = build_contestant_name_lookups(
        tournament.id, all_contestants
    )

    seats_by_user_id, team_members_by_team_id = build_hover_lookups(
        tournament, participants_by_id, teams_by_id, party.id
    )

    return {
        'party': party,
        'tournament': tournament,
        'match_data': match_data,
        'only': only,
        'match_quantities': match_quantities,
        'teams_by_id': teams_by_id,
        'participants_by_id': participants_by_id,
        'seats_by_user_id': seats_by_user_id,
        'team_members_by_team_id': team_members_by_team_id,
    }


@blueprint.get('/matches/<match_id>')
@permission_required('lan_tournament.view')
@templated
def view_match(match_id):
    """Show a match."""
    match = _get_match_or_404(match_id)
    tournament = _get_tournament_or_404(match.tournament_id)
    party = party_service.get_party(tournament.party_id)

    contestants = tournament_match_service.get_contestants_for_match(match.id)
    comments = tournament_match_service.get_comments_from_match(match.id)

    # Classify a correction before the name lookups, so these also
    # resolve the downstream contestants.
    is_ffa = is_ffa_tournament(tournament)
    ffa_result_consumed = (
        is_ffa
        and match.confirmed_by is not None
        and tournament_match_service.ffa_round_already_advanced(
            match, tournament
        )
    )
    is_walkover = is_walkover_match(contestants)
    correction_case = None
    affected_downstream_matches = []
    downstream_contestants_by_match_id = {}
    if (
        not is_ffa
        and not is_walkover
        and match.confirmed_by is not None
        and g.user.has_permission('lan_tournament.administrate')
    ):
        classification_result = (
            tournament_match_service.classify_result_correction(match.id)
        )
        if classification_result.is_ok():
            correction_case, affected_downstream_ids = (
                classification_result.unwrap()
            )
            # Keep the breadth-first order the service returned.
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
            downstream_contestants_by_match_id = (
                tournament_match_service.get_contestants_for_matches(
                    [m.id for m in affected_downstream_matches]
                )
            )

    teams_by_id, participants_by_id = build_contestant_name_lookups(
        tournament.id,
        [contestants, *downstream_contestants_by_match_id.values()],
    )

    # Resolve comment author names
    comment_user_ids = {c.created_by for c in comments}
    comment_users_by_id = user_service.get_users_indexed_by_id(comment_user_ids)

    seats_by_user_id, team_members_by_team_id = build_hover_lookups(
        tournament, participants_by_id, teams_by_id, party.id
    )

    downstream_impact = build_downstream_impact(
        affected_downstream_matches,
        downstream_contestants_by_match_id,
        correction_case,
    )

    correction_clears_winner = (
        correction_case is not None
        and retraction_reverts_completion(match, tournament)
    )

    ack_match_ids = (
        [
            str(match_id)
            for match_id in acknowledgement_match_ids(
                correction_case, affected_downstream_matches
            )
        ]
        if correction_case is not None
        else []
    )

    return {
        'party': party,
        'tournament': tournament,
        'match': match,
        'contestants': contestants,
        'comments': comments,
        'teams_by_id': teams_by_id,
        'participants_by_id': participants_by_id,
        'comment_users_by_id': comment_users_by_id,
        'seats_by_user_id': seats_by_user_id,
        'team_members_by_team_id': team_members_by_team_id,
        'match_label': build_match_label(match),
        'is_walkover': is_walkover,
        'ffa_result_consumed': ffa_result_consumed,
        'correction_case': correction_case,
        'ack_match_ids': ack_match_ids,
        'correction_clears_winner': correction_clears_winner,
        'downstream_impact': downstream_impact,
        'max_match_score': tournament_match_service.MAX_MATCH_SCORE,
    }


@blueprint.post('/matches/<match_id>/correct_result')
@permission_required('lan_tournament.administrate')
def correct_match_result(match_id):
    """Correct a match result: retract it and optionally re-enter scores."""
    match = _get_match_or_404(match_id)
    tournament = _get_tournament_or_404(match.tournament_id)

    # FFA matches have no bracket cascade to correct.
    if is_ffa_tournament(tournament):
        flash_error(
            gettext(
                'Free-for-all matches are corrected by unconfirming '
                'them and re-entering the placements.'
            )
        )
        return redirect_to('.view_match', match_id=match.id)

    form = MatchCorrectionForm(request.form)

    if not form.validate():
        flash_error(gettext('Invalid input.'))
        return redirect_to('.view_match', match_id=match_id)

    reason = form.reason.data.strip()
    ack_critical = bool(form.ack_critical.data)
    acknowledged_match_ids = parse_match_ids(
        request.form.get('ack_match_ids', '')
    )

    contestants = tournament_match_service.get_contestants_for_match(
        match.id
    )

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


@blueprint.post('/matches/<match_id>/confirm_with_scores')
@permission_required('lan_tournament.administrate')
def confirm_match_with_scores(match_id):
    """Set scores for all contestants and confirm the match."""
    match_obj = _get_match_or_404(match_id)
    match_id_obj = match_obj.id
    tournament = _get_tournament_or_404(match_obj.tournament_id)

    contestants = tournament_match_service.get_contestants_for_match(
        match_id_obj
    )

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
        match_id_obj, g.user.id, scores
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


@blueprint.post('/matches/<match_id>/unconfirm')
@permission_required('lan_tournament.administrate')
def unconfirm_match(match_id):
    """Unconfirm a match result."""
    match = _get_match_or_404(match_id)
    match_id_obj = match.id
    tournament = _get_tournament_or_404(match.tournament_id)

    # Bracket matches must use the correction, which needs an
    # acknowledgement.
    if not is_ffa_tournament(tournament):
        flash_error(
            gettext(
                'Bracket matches are retracted in the result '
                'correction panel, which requires acknowledging the '
                'impact on downstream matches.'
            )
        )
        return redirect_to('.view_match', match_id=match.id)

    form = MatchUnconfirmForm(request.form)
    if not form.validate():
        flash_error(gettext('Reason cannot be empty.'))
        return redirect_to('.view_match', match_id=match_id)

    reason = form.reason.data.strip()
    if not reason:
        flash_error(gettext('Reason cannot be empty.'))
        return redirect_to('.view_match', match_id=match_id)

    match tournament_match_service.unconfirm_match(
        match_id_obj, g.user.id, reason=reason
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


@blueprint.post('/matches/<match_id>/add_comment')
@permission_required('lan_tournament.update')
def add_match_comment(match_id):
    """Add a comment to a match."""
    match_id_obj = _get_match_or_404(match_id).id

    comment = request.form.get('comment', '').strip()

    if not comment:
        flash_error(gettext('Comment cannot be empty.'))
        return redirect_to('.view_match', match_id=match_id)

    match tournament_match_service.add_comment(
        match_id_obj, g.user.id, comment
    ):
        case Ok(_):
            flash_success(gettext('Comment has been added.'))
        case Err(error_message):
            flash_error(
                gettext(
                    'Error adding comment: %(error)s',
                    error=error_message,
                )
            )

    return redirect_to('.view_match', match_id=match_id)


@blueprint.get('/tournaments/<tournament_id>/bracket')
@permission_required('lan_tournament.view')
@templated
def bracket(tournament_id):
    """Show tournament bracket visualization."""
    tournament = _get_tournament_or_404(tournament_id)
    party = party_service.get_party(tournament.party_id)

    matches = tournament_match_service.get_matches_for_tournament_ordered(
        tournament.id
    )

    # Get contestants for each match.
    match_data = []
    all_contestants = []
    for match in matches:
        contestants = tournament_match_service.get_contestants_for_match(
            match.id
        )
        match_data.append(
            {
                'match': match,
                'contestants': contestants,
            }
        )
        all_contestants.append(contestants)

    teams_by_id, participants_by_id = build_contestant_name_lookups(
        tournament.id, all_contestants
    )

    seats_by_user_id, team_members_by_team_id = build_hover_lookups(
        tournament, participants_by_id, teams_by_id, party.id
    )

    # Tag matches that have pending feeders so the template can
    # distinguish them from true structural defwins (bye matches).
    feed_counts = compute_feed_counts(match_data)
    for entry in match_data:
        m = entry['match']
        entry['has_pending_feeder'] = (
            feed_counts.get(str(m.id), 0) > 0
            and len(entry['contestants']) <= 1
            and not m.confirmed_by
        )

    # Round-robin: compute standings table.
    standings = None
    if tournament.elimination_mode == EliminationMode.ROUND_ROBIN:
        standings = build_round_robin_standings(match_data)

    # FFA: compute cumulative and per-round standings.
    ffa_standings = None
    ffa_round_standings = None
    ffa_latest_round = None
    ffa_all_confirmed = False
    # DE-specific pool standings and status.
    ffa_wb_standings = None
    ffa_lb_standings = None
    ffa_wb_round_standings = None
    ffa_lb_round_standings = None
    ffa_gf_standings = None
    ffa_wb_latest_round = None
    ffa_lb_latest_round = None
    ffa_wb_all_confirmed = False
    ffa_lb_all_confirmed = False
    ffa_de_pool_status = None
    ffa_gf_exists = False
    ffa_gf_match_data = None
    if tournament.game_format == GameFormat.FREE_FOR_ALL:
        (
            ffa_standings,
            ffa_round_standings,
            _ffa_match_data,
            _ffa_all_contestants,
            ffa_latest_round,
            ffa_all_confirmed,
        ) = _build_ffa_round_data(tournament.id)

        # DE: compute per-pool standings and pool status.
        is_ffa_de = (
            tournament.elimination_mode
            == EliminationMode.DOUBLE_ELIMINATION
        )
        if is_ffa_de:
            pool_data = _build_ffa_de_pool_data(
                tournament.id, _ffa_match_data
            )
            ffa_wb_standings = pool_data.wb_standings
            ffa_lb_standings = pool_data.lb_standings
            ffa_gf_standings = pool_data.gf_standings
            ffa_wb_round_standings = pool_data.wb_round_standings
            ffa_lb_round_standings = pool_data.lb_round_standings
            ffa_wb_latest_round = pool_data.wb_latest_round
            ffa_lb_latest_round = pool_data.lb_latest_round
            ffa_wb_all_confirmed = pool_data.wb_all_confirmed
            ffa_lb_all_confirmed = pool_data.lb_all_confirmed
            ffa_gf_exists = pool_data.gf_exists
            ffa_gf_match_data = pool_data.gf_match_data
            ffa_de_pool_status = pool_data.pool_status

    return {
        'party': party,
        'tournament': tournament,
        'match_data': match_data,
        'standings': standings,
        'ffa_standings': ffa_standings,
        'ffa_round_standings': ffa_round_standings,
        'ffa_latest_round': ffa_latest_round,
        'ffa_all_confirmed': ffa_all_confirmed,
        'ffa_wb_standings': ffa_wb_standings,
        'ffa_lb_standings': ffa_lb_standings,
        'ffa_gf_standings': ffa_gf_standings,
        'ffa_wb_round_standings': ffa_wb_round_standings,
        'ffa_lb_round_standings': ffa_lb_round_standings,
        'ffa_wb_latest_round': ffa_wb_latest_round,
        'ffa_lb_latest_round': ffa_lb_latest_round,
        'ffa_wb_all_confirmed': ffa_wb_all_confirmed,
        'ffa_lb_all_confirmed': ffa_lb_all_confirmed,
        'ffa_de_pool_status': ffa_de_pool_status,
        'ffa_gf_exists': ffa_gf_exists,
        'ffa_gf_match_data': ffa_gf_match_data,
        'teams_by_id': teams_by_id,
        'participants_by_id': participants_by_id,
        'seats_by_user_id': seats_by_user_id,
        'team_members_by_team_id': team_members_by_team_id,
        'active_tab': 'bracket',
    }


@blueprint.post('/matches/<match_id>/comments/<comment_id>/delete')
@permission_required('lan_tournament.administrate')
def delete_match_comment(match_id, comment_id):
    """Delete a match comment."""
    match_id_obj = _get_match_or_404(match_id).id

    try:
        comment_id_obj = TournamentMatchCommentID(UUID(str(comment_id)))
    except ValueError:
        abort(404)

    match tournament_match_service.delete_comment(comment_id_obj, match_id_obj):
        case Ok():
            flash_success(gettext('Comment has been deleted.'))
        case Err(e):
            flash_error(gettext('Error deleting comment: %(error)s', error=e))

    return redirect_to('.view_match', match_id=match_id)


# -------------------------------------------------------------------- #
# highscore



def _populate_highscore_form_choices(
    form: HighscoreSubmitForm,
    tournament: Tournament,
) -> tuple[dict, dict]:
    """Populate contestant choices on a highscore form.

    Returns (teams_by_id, participants_by_id) for template rendering.
    """
    participants = (
        tournament_participant_service.get_participants_for_tournament(
            tournament.id
        )
    )
    user_ids = {p.user_id for p in participants}
    users_by_id = user_service.get_users_indexed_by_id(user_ids)

    teams_by_id: dict = {}
    participants_by_id: dict = {}

    if tournament.contestant_type == ContestantType.TEAM:
        teams = tournament_team_service.get_teams_for_tournament(
            tournament.id
        )
        teams_by_id = {t.id: t for t in teams}
        form.contestant.choices = [
            ('', gettext('-- select --')),
        ] + [(str(t.id), t.name) for t in teams]
    else:
        participants_by_id = {
            p.id: users_by_id[p.user_id]
            for p in participants
            if p.user_id in users_by_id and p.removed_at is None
        }
        form.contestant.choices = [
            ('', gettext('-- select --')),
        ] + [
            (
                str(p.id),
                users_by_id[p.user_id].screen_name,
            )
            for p in participants
            if p.user_id in users_by_id and p.removed_at is None
        ]

    return teams_by_id, participants_by_id


@blueprint.get('/tournaments/<tournament_id>/highscore')
@permission_required('lan_tournament.view')
@templated
def highscore(tournament_id):
    """Show highscore leaderboard for a tournament."""
    tournament = _get_tournament_or_404(tournament_id)

    # Only HIGHSCORE tournaments have a leaderboard.
    if tournament.game_format != GameFormat.HIGHSCORE:
        flash_error(gettext('This tournament does not use highscores.'))
        return redirect_to('.view', tournament_id=tournament.id)

    party = party_service.get_party(tournament.party_id)

    leaderboard = []
    result = tournament_score_service.get_leaderboard(tournament.id)
    match result:
        case Ok(entries):
            leaderboard = entries
        case Err(e):
            flash_error(gettext(e))

    form = HighscoreSubmitForm()
    teams_by_id, participants_by_id = _populate_highscore_form_choices(
        form, tournament
    )

    return {
        'party': party,
        'tournament': tournament,
        'leaderboard': leaderboard,
        'form': form,
        'participants_by_id': participants_by_id,
        'teams_by_id': teams_by_id,
        'active_tab': 'highscore',
    }


@blueprint.post('/tournaments/<tournament_id>/highscore/submit')
@permission_required('lan_tournament.administrate')
def highscore_submit(tournament_id):
    """Submit a score for a highscore tournament."""
    tournament = _get_tournament_or_404(tournament_id)

    if tournament.game_format != GameFormat.HIGHSCORE:
        flash_error(gettext('This tournament does not use highscores.'))
        return redirect_to('.view', tournament_id=tournament.id)

    form = HighscoreSubmitForm(request.form)

    # Populate choices so validation passes.
    _populate_highscore_form_choices(form, tournament)

    if not form.validate():
        flash_error(gettext('Invalid input.'))
        return redirect_to(
            '.highscore',
            tournament_id=tournament.id,
        )

    contestant_id = form.contestant.data
    if not contestant_id:
        flash_error(gettext('Please select a contestant.'))
        return redirect_to(
            '.highscore',
            tournament_id=tournament.id,
        )

    score_value = form.score.data
    note = form.note.data.strip() if form.note.data else None

    from byceps.services.lan_tournament.models.tournament_participant import (
        TournamentParticipantID,
    )

    participant_id = None
    team_id = None
    if tournament.contestant_type == ContestantType.TEAM:
        team_id = TournamentTeamID(UUID(contestant_id))
    else:
        participant_id = TournamentParticipantID(UUID(contestant_id))

    match tournament_score_service.submit_score(
        tournament.id,
        score_value,
        participant_id=participant_id,
        team_id=team_id,
        submitted_by=g.user.id,
        note=note,
    ):
        case Ok(_):
            flash_success(gettext('Score has been submitted.'))
        case Err(error_message):
            flash_error(gettext(error_message))

    return redirect_to('.highscore', tournament_id=tournament.id)


@blueprint.post('/tournaments/<tournament_id>/highscore/delete-all')
@permission_required('lan_tournament.administrate')
def highscore_delete_all(tournament_id):
    """Delete all scores for a highscore tournament."""
    tournament = _get_tournament_or_404(tournament_id)

    if tournament.game_format != GameFormat.HIGHSCORE:
        flash_error(gettext('This tournament does not use highscores.'))
        return redirect_to('.view', tournament_id=tournament.id)

    match tournament_score_service.delete_scores_for_tournament(
        tournament.id,
    ):
        case Ok(_):
            flash_success(gettext('All scores have been deleted.'))
        case Err(error_message):
            flash_error(gettext(error_message))

    return redirect_to('.highscore', tournament_id=tournament.id)


# -------------------------------------------------------------------- #
# FFA (Free-for-All) endpoints
# -------------------------------------------------------------------- #


def _build_ffa_match_data_list(tournament_id: TournamentID):
    """Build a flat list of {match, contestants} dicts for all FFA matches."""
    matches = tournament_match_service.get_matches_for_tournament_ordered(
        tournament_id
    )
    match_data = []
    for m in matches:
        contestants = tournament_match_service.get_contestants_for_match(m.id)
        match_data.append({'match': m, 'contestants': contestants})
    return match_data


def _build_ffa_round_data(tournament_id: TournamentID):
    """Build per-round and cumulative FFA standings from match data.

    Returns (cumulative_standings, round_standings, match_data,
    all_contestants, latest_round, all_confirmed).
    """
    matches = tournament_match_service.get_matches_for_tournament_ordered(
        tournament_id
    )

    # Group matches by round.
    rounds_map: dict[int, list] = {}
    match_data = []
    all_contestants: list[list] = []
    for m in matches:
        contestants = tournament_match_service.get_contestants_for_match(m.id)
        match_data.append({'match': m, 'contestants': contestants})
        all_contestants.append(contestants)

        rn = m.round if m.round is not None else 0
        rounds_map.setdefault(rn, []).append(contestants)

    # Build per-round standings.
    round_standings = []
    for rn in sorted(rounds_map.keys()):
        rs = tournament_domain_service.compute_ffa_round_standings(
            rounds_map[rn]
        )
        round_standings.append({'round_num': rn, 'standings': rs})

    # Build cumulative standings.
    all_round_matches = [rounds_map[rn] for rn in sorted(rounds_map.keys())]
    cumulative = tournament_domain_service.compute_ffa_cumulative_standings(
        all_round_matches
    )

    # Determine latest round and whether all matches are confirmed.
    latest_round = max(rounds_map.keys()) if rounds_map else None
    all_confirmed = True
    if latest_round is not None:
        for entry in match_data:
            if (entry['match'].round == latest_round
                    and entry['match'].confirmed_by is None):
                all_confirmed = False
                break

    return (
        cumulative,
        round_standings,
        match_data,
        all_contestants,
        latest_round,
        all_confirmed,
    )


@blueprint.get('/tournaments/<tournament_id>/ffa_standings')
@permission_required('lan_tournament.view')
@templated
def ffa_standings(tournament_id):
    """Show FFA cumulative and per-round standings."""
    tournament = _get_tournament_or_404(tournament_id)

    if tournament.game_format != GameFormat.FREE_FOR_ALL:
        flash_error(gettext('This tournament is not a Free-for-All format.'))
        return redirect_to('.view', tournament_id=tournament.id)

    party = party_service.get_party(tournament.party_id)

    (
        cumulative,
        round_standings,
        match_data,
        all_contestants,
        latest_round,
        all_confirmed,
    ) = _build_ffa_round_data(tournament.id)

    teams_by_id, participants_by_id = build_contestant_name_lookups(
        tournament.id, all_contestants
    )

    seats_by_user_id, team_members_by_team_id = build_hover_lookups(
        tournament, participants_by_id, teams_by_id, party.id
    )

    return {
        'party': party,
        'tournament': tournament,
        'standings': cumulative,
        'round_standings': round_standings,
        'latest_round': latest_round,
        'all_confirmed': all_confirmed,
        'teams_by_id': teams_by_id,
        'participants_by_id': participants_by_id,
        'seats_by_user_id': seats_by_user_id,
        'team_members_by_team_id': team_members_by_team_id,
        'active_tab': 'bracket',
    }


@blueprint.post('/tournaments/<tournament_id>/generate_ffa_round')
@permission_required('lan_tournament.administrate')
def generate_ffa_round_action(tournament_id):
    """Generate the next FFA round."""
    tournament = _get_tournament_or_404(tournament_id)

    if tournament.game_format != GameFormat.FREE_FOR_ALL:
        flash_error(gettext('This tournament is not a Free-for-All format.'))
        return redirect_to('.view', tournament_id=tournament.id)

    # DE tournaments start in the Winners bracket.
    bracket = None
    if tournament.elimination_mode == EliminationMode.DOUBLE_ELIMINATION:
        bracket = Bracket.WINNERS

    # Round number is determined inside the service under the
    # tournament lock to avoid TOCTOU races.
    result = tournament_match_service.generate_ffa_round(
        tournament.id,
        bracket=bracket,
        initiator_id=g.user.id,
    )

    match result:
        case Ok(match_count):
            flash_success(
                gettext(
                    'FFA round generated with %(count)d group(s).',
                    count=match_count,
                )
            )
        case Err(error_message):
            flash_error(
                gettext(
                    'FFA round generation failed: %(error)s',
                    error=error_message,
                )
            )

    return redirect_to('.bracket', tournament_id=tournament.id)


@blueprint.post('/tournaments/<tournament_id>/advance_ffa_round')
@permission_required('lan_tournament.administrate')
def advance_ffa_round_action(tournament_id):
    """Advance FFA tournament to the next round.

    For DE tournaments, the ``pool`` POST parameter selects which pool
    to advance (``WB`` or ``LB``).
    """
    tournament = _get_tournament_or_404(tournament_id)

    if tournament.game_format != GameFormat.FREE_FOR_ALL:
        flash_error(gettext('This tournament is not a Free-for-All format.'))
        return redirect_to('.view', tournament_id=tournament.id)

    # DE: read pool parameter from form POST data.
    pool = None
    is_de = (
        tournament.elimination_mode
        == EliminationMode.DOUBLE_ELIMINATION
    )
    if is_de:
        pool_value = request.form.get('pool', '').strip()
        if pool_value == 'WB':
            pool = Bracket.WINNERS
        elif pool_value == 'LB':
            pool = Bracket.LOSERS
        else:
            flash_error(
                gettext(
                    'Invalid pool parameter. Use WB or LB.'
                )
            )
            return redirect_to('.bracket', tournament_id=tournament.id)

    result = tournament_match_service.advance_ffa_round(
        tournament.id,
        pool=pool,
        initiator_id=g.user.id,
    )

    match result:
        case Ok(value):
            if isinstance(value, str):
                # Signal values: 'grand_final_eligible',
                # 'advanced_wb', 'advanced_lb'
                if value == 'grand_final_eligible':
                    flash_success(
                        gettext(
                            'Grand Final conditions met. Generate the '
                            'Grand Final round.'
                        )
                    )
                else:
                    flash_success(
                        gettext(
                            'Round advanced: %(signal)s',
                            signal=value,
                        )
                    )
            else:
                flash_success(
                    gettext(
                        'Advanced to next round. %(count)d group(s) '
                        'generated.',
                        count=value,
                    )
                )
        case Err(error_message):
            flash_error(
                gettext(
                    'Round advancement failed: %(error)s',
                    error=error_message,
                )
            )

    return redirect_to('.bracket', tournament_id=tournament.id)


# Placements ARE the result of a free-for-all match -- they decide
# its winner and the points that feed the standings, and since the
# bracket score path refuses an FFA match
# (PLACEMENT_FORMAT_CONFIRM_ERROR) they are the only way to enter
# one. So this takes the same permission every other result-entry
# route takes, not the metadata-editing 'update'.
@blueprint.post('/matches/<match_id>/set_ffa_placements')
@permission_required('lan_tournament.administrate')
def set_ffa_placements_action(match_id):
    """Set FFA placements for all contestants in a match."""
    match_obj = _get_match_or_404(match_id)
    match_id_obj = TournamentMatchID(match_obj.id)

    parse_result = parse_submitted_ffa_placements(request.form)
    if parse_result.is_err():
        flash_error(parse_result.unwrap_err())
        return redirect_to('.view_match', match_id=match_id)

    result = tournament_match_service.set_ffa_placements(
        match_id_obj,
        parse_result.unwrap(),
    )

    match result:
        case Ok(_):
            flash_success(gettext('Placements have been set.'))
        case Err(error_message):
            flash_error(
                gettext(
                    'Error setting placements: %(error)s',
                    error=gettext(error_message),
                )
            )

    return redirect_to('.view_match', match_id=match_id)


@blueprint.post('/matches/<match_id>/confirm_ffa')
@permission_required('lan_tournament.administrate')
def confirm_ffa_match_action(match_id):
    """Confirm an FFA match after placements are set."""
    match_obj = _get_match_or_404(match_id)
    match_id_obj = TournamentMatchID(match_obj.id)

    result = tournament_match_service.confirm_ffa_match(
        match_id_obj,
        g.user.id,
    )

    match result:
        case Ok(_):
            flash_success(gettext('FFA match has been confirmed.'))
        case Err(error_message):
            flash_error(
                gettext(
                    'Error confirming FFA match: %(error)s',
                    error=error_message,
                )
            )

    return redirect_to('.view_match', match_id=match_id)


@blueprint.post('/tournaments/<tournament_id>/generate_ffa_grand_final')
@permission_required('lan_tournament.administrate')
def generate_ffa_grand_final_action(tournament_id):
    """Generate the Grand Final round for an FFA-DE tournament."""
    tournament = _get_tournament_or_404(tournament_id)

    if tournament.game_format != GameFormat.FREE_FOR_ALL:
        flash_error(gettext('This tournament is not a Free-for-All format.'))
        return redirect_to('.view', tournament_id=tournament.id)

    if tournament.elimination_mode != EliminationMode.DOUBLE_ELIMINATION:
        flash_error(
            gettext('Grand Final is only for double elimination tournaments.')
        )
        return redirect_to('.view', tournament_id=tournament.id)

    result = tournament_match_service.generate_ffa_grand_final(
        tournament.id,
        initiator_id=g.user.id,
    )

    match result:
        case Ok(match_count):
            flash_success(
                gettext(
                    'Grand Final generated with %(count)d group(s).',
                    count=match_count,
                )
            )
        case Err(error_message):
            flash_error(
                gettext(
                    'Grand Final generation failed: %(error)s',
                    error=error_message,
                )
            )

    return redirect_to('.bracket', tournament_id=tournament.id)


def _build_ffa_de_pool_data(tournament_id, ffa_match_data):
    """Build per-pool standings and status for FFA-DE tournaments."""
    # Partition matches by bracket/pool.
    wb_rounds_map: dict[int, list] = {}
    lb_rounds_map: dict[int, list] = {}
    gf_groups: list[list] = []
    gf_match_entries: list[dict] = []

    # Track contestants per pool and round for status counts.
    # We use per-round sets so we can compute *current* pool membership
    # (latest round only), not historical.
    wb_contestants_per_round: dict[int, set[str]] = {}
    lb_contestants_per_round: dict[int, set[str]] = {}
    gf_contestant_ids: set[str] = set()
    all_contestant_ids: set[str] = set()

    for entry in ffa_match_data:
        m = entry['match']
        contestants = entry['contestants']
        rn = m.round if m.round is not None else 0

        # Collect all contestant IDs for overall tracking.
        for c in contestants:
            all_contestant_ids.add(tournament_domain_service.contestant_id(c))

        if m.bracket == Bracket.WINNERS:
            wb_rounds_map.setdefault(rn, []).append(contestants)
            wb_contestants_per_round.setdefault(rn, set())
            for c in contestants:
                wb_contestants_per_round[rn].add(tournament_domain_service.contestant_id(c))
        elif m.bracket == Bracket.LOSERS:
            lb_rounds_map.setdefault(rn, []).append(contestants)
            lb_contestants_per_round.setdefault(rn, set())
            for c in contestants:
                lb_contestants_per_round[rn].add(tournament_domain_service.contestant_id(c))
        elif m.bracket == Bracket.GRAND_FINAL:
            gf_groups.append(contestants)
            gf_match_entries.append(entry)
            for c in contestants:
                gf_contestant_ids.add(tournament_domain_service.contestant_id(c))

    # WB standings.
    wb_round_standings = []
    for rn in sorted(wb_rounds_map.keys()):
        rs = tournament_domain_service.compute_ffa_round_standings(
            wb_rounds_map[rn]
        )
        wb_round_standings.append({'round_num': rn, 'standings': rs})

    wb_all_rounds = [
        wb_rounds_map[rn] for rn in sorted(wb_rounds_map.keys())
    ]
    wb_standings = tournament_domain_service.compute_ffa_cumulative_standings(
        wb_all_rounds
    ) if wb_all_rounds else []

    # LB standings.
    lb_round_standings = []
    for rn in sorted(lb_rounds_map.keys()):
        rs = tournament_domain_service.compute_ffa_round_standings(
            lb_rounds_map[rn]
        )
        lb_round_standings.append({'round_num': rn, 'standings': rs})

    lb_all_rounds = [
        lb_rounds_map[rn] for rn in sorted(lb_rounds_map.keys())
    ]
    lb_standings = tournament_domain_service.compute_ffa_cumulative_standings(
        lb_all_rounds
    ) if lb_all_rounds else []

    # GF standings.
    gf_standings = tournament_domain_service.compute_ffa_cumulative_standings(
        [gf_groups]
    ) if gf_groups else []

    # Latest round numbers.
    wb_latest_round = max(wb_rounds_map.keys()) if wb_rounds_map else None
    lb_latest_round = max(lb_rounds_map.keys()) if lb_rounds_map else None

    # Confirmed status per pool.
    wb_all_confirmed = True
    if wb_latest_round is not None:
        for entry in ffa_match_data:
            m = entry['match']
            if (m.bracket == Bracket.WINNERS
                    and m.round == wb_latest_round
                    and m.confirmed_by is None):
                wb_all_confirmed = False
                break

    lb_all_confirmed = True
    if lb_latest_round is not None:
        for entry in ffa_match_data:
            m = entry['match']
            if (m.bracket == Bracket.LOSERS
                    and m.round == lb_latest_round
                    and m.confirmed_by is None):
                lb_all_confirmed = False
                break

    gf_exists = len(gf_groups) > 0

    # Pool status: count *current* pool membership using latest round.
    current_wb = (
        wb_contestants_per_round.get(wb_latest_round, set())
        if wb_latest_round is not None
        else set()
    )
    current_lb = (
        lb_contestants_per_round.get(lb_latest_round, set())
        if lb_latest_round is not None
        else set()
    )
    active_ids = current_wb | current_lb | gf_contestant_ids
    pool_status = {
        'wb_count': len(current_wb),
        'lb_count': len(current_lb),
        'gf_count': len(gf_contestant_ids),
        'eliminated_count': max(
            0,
            len(all_contestant_ids) - len(active_ids),
        ) if all_contestant_ids else 0,
        'total': len(all_contestant_ids),
    }

    return FfaDePoolData(
        wb_standings=wb_standings,
        lb_standings=lb_standings,
        gf_standings=gf_standings,
        wb_round_standings=wb_round_standings,
        lb_round_standings=lb_round_standings,
        wb_latest_round=wb_latest_round,
        lb_latest_round=lb_latest_round,
        wb_all_confirmed=wb_all_confirmed,
        lb_all_confirmed=lb_all_confirmed,
        gf_exists=gf_exists,
        gf_match_data=gf_match_entries,
        pool_status=pool_status,
    )
