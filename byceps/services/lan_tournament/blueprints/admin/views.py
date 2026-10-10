import base64
from collections import Counter
from contextlib import suppress
import dataclasses
from datetime import datetime, UTC
from enum import Enum
from functools import wraps
from io import BytesIO
import math
from uuid import UUID, uuid4, uuid5

from flask import (
    abort,
    current_app,
    g,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from flask_babel import (
    format_date,
    format_datetime,
    format_decimal,
    format_time,
    format_timedelta,
    get_locale,
    gettext,
    ngettext,
    to_user_timezone,
    to_utc,
)
from markupsafe import Markup
from werkzeug.exceptions import RequestEntityTooLarge

from byceps.services.brand import brand_service
from byceps.services.party import party_service
from byceps.services.party.models import Party, PartyID
from byceps.services.user import user_service
from byceps.services.user.models import UserID
from byceps.util.framework.blueprint import (
    create_blueprint,
    register_blueprints,
)
from byceps.util.framework.flash import (
    flash_error,
    flash_notice,
    flash_success,
)
from byceps.util.framework.templating import templated
from byceps.util.result import Err, Ok
from byceps.util.views import (
    login_required,
    permission_required,
    redirect_to,
    respond_no_content,
)

from byceps.services.lan_tournament import (
    tournament_config_document,
    tournament_config_domain_service,
    tournament_config_service,
    tournament_domain_service,
    tournament_image_service,
    tournament_maintenance_service,
    tournament_match_service,
    tournament_notification_service,
    tournament_orga_service,
    tournament_participant_service,
    tournament_qualification_repository,
    tournament_qualification_service,
    tournament_repository,
    tournament_request_domain_service,
    tournament_request_repository,
    tournament_request_service,
    tournament_score_service,
    tournament_seeding_service,
    tournament_service,
    tournament_stats_service,
    tournament_team_service,
)
from byceps.services.lan_tournament.models.tournament import (
    Tournament,
    TournamentID,
)
from byceps.services.lan_tournament.models.tournament_category import (
    TournamentCategory,
)
from byceps.services.lan_tournament.models.tournament_dashboard import (
    DashboardQuery,
    DashboardSettings,
)
from byceps.services.lan_tournament.models.tournament_image import (
    TournamentImage,
    TournamentImageID,
)
from byceps.services.lan_tournament.models.validation_message import (
    ValidationMessage,
)
from byceps.services.lan_tournament.models.tournament_request import (
    TournamentRequest,
    TournamentRequestID,
    TournamentRequestStatus,
)
from byceps.services.lan_tournament.models.contestant_type import (
    ContestantType,
)
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipantID,
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
from byceps.services.lan_tournament.models.match_readiness import (
    real_contestants,
    side_for_contestant,
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
    active_match_filter,
    build_contestant_name_lookups,
    contestant_names,
    build_create_wizard_context,
    build_downstream_impact,
    build_hover_lookups,
    build_match_label,
    build_match_readiness_projections,
    build_request_refusal,
    build_round_robin_standings,
    build_seat_lookup,
    build_team_members_lookup,
    compute_feed_counts,
    count_match_projections,
    filter_match_projections,
    first_error_step,
    format_file_size,
    get_timezone_detail_at,
    ffa_elimination_mode,
    ffa_grand_final_offer,
    ffa_grand_final_refusal,
    ffa_phase,
    group_tournaments_by_category,
    is_walkover_match,
    match_filter_options,
    match_uses_placements,
    participant_rankings,
    parse_match_ids,
    phase_match_labels,
    serialize_public_match_readiness,
    parse_submitted_contestant_scores,
    parse_int,
    parse_seeding_action,
    parse_submitted_ffa_placements,
    playoff_waiting_reason,
    ffa_cut_ties_payload,
    qualification_js_strings,
    qualification_strings,
    playoff_board,
    playoff_release_open,
    generation_flash,
    seeding_audit_context,
    start_gate,
    START_CONFIRM_FIELD,
    seeding_board_payload,
    leaderboard_submission_times,
    seeding_error_status,
    separation_message,
    serialize_qualification,
    wants_json,
)
from byceps.services.lan_tournament.blueprints.dashboard_csrf import (
    CSRF_INVALID_ERROR,
    CSRF_INVALID_NOTICE,
    get_dashboard_csrf_token,
    validate_dashboard_csrf,
)
from byceps.services.lan_tournament.blueprints.dashboard_forms import (
    DashboardAcknowledgementForm,
    DashboardPinForm,
    MAX_COMMENT_LENGTH,
    parse_dashboard_revision,
)
from byceps.services.lan_tournament.dashboard_config import (
    INVALID_RED_MINUTES_ERROR,
    INVALID_THRESHOLD_ORDER_ERROR,
    INVALID_YELLOW_MINUTES_ERROR,
    MAX_THRESHOLD_MINUTES,
    get_dashboard_settings,
)
from byceps.services.lan_tournament.dashboard_view_helpers import (
    TRANSPORT_ERROR_ACCESS_REVOKED,
    TRANSPORT_ERROR_CSRF_INVALID,
    TRANSPORT_ERROR_INVALID,
    TRANSPORT_ERROR_REFUSED,
    TRANSPORT_ERROR_SESSION_EXPIRED,
    TRANSPORT_ERROR_STALE,
    TRANSPORT_ERROR_UNAVAILABLE,
    TRANSPORT_ERRORS,
    build_dashboard_context,
    build_dashboard_list_url,
    dashboard_labels,
    describe_dashboard_return,
    format_comment_counter,
    format_freshness_time,
    format_wall_time,
    parse_dashboard_query,
    parse_dashboard_return,
    serialize_dashboard_error,
    serialize_dashboard_fragment,
    serialize_dashboard_success,
)
from byceps.services.lan_tournament.tournament_dashboard_coordination_service import (
    DASHBOARD_ACK_CONFLICT_ERROR,
    acknowledge_match,
    set_match_pin,
)
from byceps.services.lan_tournament.tournament_dashboard_service import (
    DASHBOARD_FORBIDDEN_ERROR,
    DASHBOARD_QUERY_INVALID_ERROR,
    DASHBOARD_UNAUTHENTICATED_ERROR,
    get_dashboard_page,
    resolve_dashboard_scope,
)
from byceps.services.lan_tournament.tournament_dashboard_settings_service import (
    THRESHOLDS_STALE_ERROR,
    get_effective_dashboard_settings,
    get_party_thresholds,
    reset_party_thresholds,
    set_party_thresholds,
)
from byceps.services.more.blueprints.admin import item_service
from byceps.services.more.blueprints.admin.item_service import MoreItem
from byceps.services.lan_tournament.tournament_overview_filters import (
    available_categories,
    parse_assignment,
    parse_category,
)

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
    TournamentImportForm,
    TournamentOrgaAssignForm,
    TournamentRequestRejectForm,
    TournamentRequestUpdateForm,
    TournamentUpdateForm,
    _REQUEST_ELIMINATION_MODE_LABELS,
    min_above_max_error,
)


blueprint = create_blueprint('lan_tournament_admin', __name__)
register_blueprints(
    blueprint, [('services.lan_tournament.blueprints.common', None)]
)


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
    try:
        category_filter = parse_category(request.args.get('category', 'ALL'))
    except ValueError:
        abort(400)
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
        'tournament_groups': group_tournaments_by_category(tournaments),
        'stats': stats,
        'participant_counts': participant_counts,
        'email_templates_configured': email_templates_configured,
        'categories': available_categories(tournaments),
        'category_filter': category_filter,
        'category_filter_endpoint': '.overview',
        'category_filter_args': {'party_id': party.id},
        'filtered_count': sum(
            1
            for t in tournaments
            if category_filter is None or t.category == category_filter
        ),
    }


@blueprint.get('/for_party/<party_id>')
@permission_required('lan_tournament.view')
@templated
def index(party_id):
    """List tournaments for that party."""
    party = _get_party_or_404(party_id)

    tournaments = tournament_service.get_tournaments_for_party(party.id)

    try:
        assignment_filter = parse_assignment(
            request.args.get('assignment', 'all')
        )
        category_filter = parse_category(request.args.get('category', 'ALL'))
    except ValueError:
        abort(400)
    if assignment_filter == 'mine':
        assigned_ids = tournament_orga_service.get_tournament_ids_for_orga(
            g.user.id
        )
        tournaments = [t for t in tournaments if t.id in assigned_ids]

    total_count = len(tournaments)
    tournament_ids = [t.id for t in tournaments]
    participant_counts = tournament_service.get_participant_counts_for_tournaments(tournament_ids)
    team_counts = tournament_team_service.get_team_counts_for_tournaments(tournament_ids)

    return {
        'party': party,
        'tournaments': tournaments,
        'tournament_groups': group_tournaments_by_category(tournaments),
        'participant_counts': participant_counts,
        'team_counts': team_counts,
        'elimination_mode_labels': _build_elimination_mode_labels(),
        'assignment_filter': assignment_filter,
        'categories': available_categories(tournaments),
        'category_filter_endpoint': '.index',
        'category_filter_args': {
            'party_id': party.id,
            'assignment': assignment_filter,
        },
        'category_filter': category_filter,
        'total_count': total_count,
        'filtered_count': sum(
            1
            for t in tournaments
            if category_filter is None or t.category == category_filter
        ),
    }


@blueprint.post('/for_party/<party_id>/sort')
@permission_required('lan_tournament.update')
@respond_no_content
def sort_tournaments(party_id):
    """Reorder tournaments for that party."""
    _get_party_or_404(party_id)

    data = request.get_json(silent=True)
    if not isinstance(data, dict) or 'tournament_ids' not in data:
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

    match_data, readiness_by_match_id = _admin_match_projections(tournament)
    match_quantities = count_match_projections(
        list(readiness_by_match_id.values())
    )
    match_labels = (
        phase_match_labels(tournament, [entry['match'] for entry in match_data])
        if tournament.has_playoffs else {}
    )
    teams_by_id, participants_by_id = build_contestant_name_lookups(
        tournament.id, [entry['contestants'] for entry in match_data]
    )

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
    ffa_mode = ffa_elimination_mode(tournament)
    is_ffa_de = ffa_mode == EliminationMode.DOUBLE_ELIMINATION
    if is_ffa_de and has_bracket:
        pool_data = _build_ffa_de_pool_data(
            tournament.id,
            match_data,
        )
        ffa_de_pool_status = pool_data.pool_status
        offer = ffa_grand_final_offer(tournament)
        ffa_gf_eligible = offer is not None and offer['state'] == 'ready'

    return {
        'party': party,
        'tournament': tournament,
        'has_bracket': has_bracket,
        'match_data': match_data,
        'readiness_by_match_id': readiness_by_match_id,
        'match_quantities': match_quantities,
        'match_filter_options': match_filter_options(match_quantities),
        'only': 'all',
        'match_labels': match_labels,
        'teams_by_id': teams_by_id,
        'participants_by_id': participants_by_id,
        'requires_bracket': tournament.game_format.requires_bracket_generation if tournament.game_format else False,
        'start_gate': start_gate(tournament),
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
        'ffa_phase': ffa_phase(tournament),
        'ffa_mode': ffa_mode,
        'start_time_zone': (
            f'{current_app.config["TIMEZONE"]}, '
            f'{get_timezone_detail_at(tournament.start_time)}'
            if tournament.start_time
            else None
        ),
        'active_tab': 'overview',
        'dashboard_back': _dashboard_back_link(tournament),
    }


@blueprint.get('/for_party/<party_id>/create')
@permission_required('lan_tournament.create')
@templated
def create_form(
    party_id, erroneous_form=None, image_error=None, refused_request=None
):
    """Show form to create a tournament."""
    party = _get_party_or_404(party_id)

    # Set below whenever the form on screen was (or was meant to be)
    # prefilled from a request, so the template can show a provenance
    # banner and link back to it. Stays `None` for a plain create and
    # for a request that turned out not to be prefillable -- the
    # banner would otherwise claim a prefill that never happened.
    source_request = None

    if erroneous_form:
        form = erroneous_form
        # Same permission gate as the GET prefill branch below: a
        # forged hidden field must not let a `create`-only admin learn
        # about a request they cannot view/decide, just because their
        # submission happened to fail validation.
        if (
            erroneous_form.from_request_id.data
            and g.user.has_permission('lan_tournament.request_view')
            and g.user.has_permission('lan_tournament.request_decide')
        ):
            found_request = _find_request_for_party(
                erroneous_form.from_request_id.data, party.id
            )
            # Same recreatability check as the GET prefill branch
            # below: a request that moved on (decided by another
            # admin, withdrawn) since this render's inputs were built
            # must not keep showing a banner that claims a prefill
            # `create`'s own re-check would refuse.
            if found_request is not None and _is_request_recreatable(
                found_request
            ):
                source_request = found_request
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
                found_request = _find_request_for_party(
                    from_request_raw, party.id
                )
                if found_request is not None:
                    if _is_request_recreatable(found_request):
                        _prefill_form_from_request(form, found_request)
                        source_request = found_request
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

    if image_error:
        _add_field_error(form.image, image_error)

    form.set_contestant_type_choices()
    form.set_game_format_choices()
    form.set_elimination_mode_choices()
    form.set_score_ordering_choices()

    source_proposer_name = None
    source_request_blocking_field_labels: list[str] = []
    if source_request is not None:
        source_proposer_name = user_service.find_screen_name(
            source_request.proposer_id
        )
        gap = tournament_request_domain_service.analyze_field_gap(
            source_request
        )
        source_request_blocking_field_labels = [
            str(getattr(form, field_name).label.text)
            for field_name in gap.blocking
        ]

    refusal = None
    refused_proposer_name = None
    if refused_request is not None and source_request is None:
        refusal = _build_refusal(refused_request)
        refused_proposer_name = refusal['proposerName']

    if not form.submission_token.data:
        form.submission_token.data = str(uuid4())

    staged_image = _find_staged_image(form.image_id.data, party)

    return {
        'party': party,
        'form': form,
        'source_request': source_request,
        'source_proposer_name': source_proposer_name,
        'refused_request': refused_request if refusal else None,
        'refused_proposer_name': refused_proposer_name,
        'source_request_blocking_field_labels': (
            source_request_blocking_field_labels
        ),
        'wizard': build_create_wizard_context(
            party,
            form,
            source_request=source_request,
            source_proposer_name=source_proposer_name,
            staged_image=staged_image,
            urls=_create_wizard_urls(party),
            refusal=refusal,
        ),
    }


@dataclasses.dataclass(frozen=True, kw_only=True)
class _CreateSubmission:
    name: str
    category: TournamentCategory
    game: str | None
    description: str | None
    image_url: str | None
    ruleset: str | None
    start_time: datetime | None
    settings: tournament_domain_service.TournamentSettings
    points_carry_to_losers: bool | None
    source_request: TournamentRequest | None
    image_id: TournamentImageID | None
    image_alt_text: str | None
    creation_token: UUID | None


@blueprint.post('/for_party/<party_id>')
@permission_required('lan_tournament.create')
def create(party_id):
    """Create a tournament."""
    request.max_content_length = tournament_image_service.MAX_REQUEST_BYTES

    party = _get_party_or_404(party_id)

    try:
        formdata = _get_create_formdata()
    except RequestEntityTooLarge:
        # The entries are in the unparsed body and cannot be restored.
        body = create_form(party.id, image_error=_translate_image_too_large())
        return body, 413

    form = _build_create_form(formdata)

    raw_token = form.submission_token.data
    creation_token, existing = _resolve_creation_token(raw_token, party)
    if creation_token != _parse_uuid(raw_token):
        form.submission_token.data = str(creation_token)
    if existing is not None:
        flash_notice(gettext('This tournament has already been created.'))
        return redirect_to('.view', tournament_id=existing.id)

    if not form.validate():
        return create_form(party.id, form)

    sub = _parse_create_submission(form, party)
    if sub is None:
        refused_request = None
        if form.from_request_id.errors:
            refused_request = _find_refused_request(
                form.from_request_id.data, party
            )
            # A resubmit becomes an unlinked create, not an endless refusal.
            _clear_stale_request_link(form)
        if form.image_id.errors:
            form.image_id.data = ''
        return create_form(party.id, form, refused_request=refused_request)

    image_id = sub.image_id
    upload = form.image.data
    if upload is not None and getattr(upload, 'filename', None):
        match tournament_image_service.store_uploaded_image(
            party.id, g.user.id, upload.stream, upload.filename
        ):
            case Ok(image):
                image_id = image.id
                # A file input cannot be refilled: keep the staged image.
                form.image_id.data = str(image.id)
            case Err(message):
                _add_field_error(
                    form.image, _translate_image_message(message)
                )
                return create_form(party.id, form)

    settings = sub.settings
    result = tournament_service.create_tournament(
        party.id,
        sub.name,
        category=sub.category,
        game=sub.game,
        description=sub.description,
        image_url=sub.image_url,
        ruleset=sub.ruleset,
        start_time=sub.start_time,
        min_players=settings.min_players,
        max_players=settings.max_players,
        min_teams=settings.min_teams,
        max_teams=settings.max_teams,
        min_players_in_team=settings.min_players_in_team,
        max_players_in_team=settings.max_players_in_team,
        contestant_type=settings.contestant_type,
        tournament_status=TournamentStatus.DRAFT,
        game_format=settings.game_format,
        elimination_mode=settings.elimination_mode,
        score_ordering=settings.score_ordering,
        point_table=settings.point_table,
        advancement_count=settings.advancement_count,
        group_size_min=settings.group_size_min,
        group_size_max=settings.group_size_max,
        points_carry_to_losers=sub.points_carry_to_losers,
        playoff_game_format=settings.playoff_game_format,
        playoff_elimination_mode=settings.playoff_elimination_mode,
        playoff_group_count=settings.playoff_group_count,
        playoff_qualifiers_per_group=settings.playoff_qualifiers_per_group,
        playoff_qualifier_count=settings.playoff_qualifier_count,
        playoff_release_mode=settings.playoff_release_mode,
        created_from_request_id=(
            sub.source_request.id if sub.source_request else None
        ),
        initiator_id=g.user.id,
        image_id=image_id,
        image_alt_text=sub.image_alt_text,
        creation_token=sub.creation_token,
    )
    if result.is_err():
        error_message = result.unwrap_err()

        # A concurrent duplicate may trip another unique index first.
        existing = (
            tournament_service.find_tournament_by_creation_token(
                sub.creation_token
            )
            if sub.creation_token is not None
            else None
        )
        if existing is not None and existing.party_id == party.id:
            flash_notice(gettext('This tournament has already been created.'))
            return redirect_to('.view', tournament_id=existing.id)

        if error_message == tournament_service.DUPLICATE_SUBMISSION_ERROR:
            form.form_errors.append(gettext(error_message))
        elif error_message == tournament_service.IMAGE_UNAVAILABLE_ERROR:
            _add_field_error(form.image_id, gettext(error_message))
            form.image_id.data = ''
        elif sub.source_request is not None and error_message in (
            'Request is no longer in the expected state.',
            'A tournament has already been created from this request.',
        ):
            # The row-locked re-check inside create_tournament caught
            # what the fast path in the parser missed.
            _add_field_error(form.from_request_id, gettext(error_message))
            refused_request = _find_refused_request(
                form.from_request_id.data, party
            )
            _clear_stale_request_link(form)
            return create_form(party.id, form, refused_request=refused_request)
        else:
            form.form_errors.append(_translate_error(error_message))
        return create_form(party.id, form)

    tournament, _event = result.unwrap()

    if sub.source_request is not None:
        # The request is already linked at this point -- create_tournament
        # committed both in one transaction. This is best-effort only:
        # a failure never rolls back the tournament that already exists.
        orga_result = tournament_request_service.appoint_proposer_orga(
            tournament.id, sub.source_request.proposer_id, g.user.id
        )
        if orga_result.is_err():
            flash_notice(gettext(orga_result.unwrap_err()))

    flash_success(
        gettext(
            'Tournament "%(name)s" has been created as a draft.',
            name=tournament.name,
        )
    )

    return redirect_to('.view', tournament_id=tournament.id)


@blueprint.post('/for_party/<party_id>/create/validate')
@permission_required('lan_tournament.create')
def validate_create(party_id):
    """Run create validation without writing; return JSON."""
    request.max_content_length = tournament_image_service.MAX_REQUEST_BYTES

    party = _get_party_or_404(party_id)

    try:
        formdata = request.form
    except RequestEntityTooLarge:
        return _json_error(_translate_image_too_large(), 413)

    form = _build_create_form(formdata)
    form.validate()
    _parse_create_submission(form, party)

    refusal = None
    if form.from_request_id.errors:
        refused_request = _find_refused_request(
            form.from_request_id.data, party
        )
        if refused_request is not None:
            refusal = _build_refusal(refused_request)

    errors = form.errors
    return jsonify(
        ok=not errors,
        errors={
            key: [str(message) for message in messages]
            for key, messages in errors.items()
        },
        first_error_step=first_error_step(form),
        checked_at=format_time(datetime.now(UTC), 'HH:mm'),
        refusal=refusal,
    )


@blueprint.get('/for_party/<party_id>/import')
@permission_required('lan_tournament.create')
@templated
def import_form(
    party_id,
    erroneous_form=None,
    *,
    summary_rows=None,
    problems=None,
    problems_truncated=False,
    document_b64=None,
):
    """Show form to import a tournament configuration."""
    party = _get_party_or_404(party_id)

    form = erroneous_form
    if form is None:
        form = TournamentImportForm()
    if not form.submission_token.data:
        form.submission_token.data = str(uuid4())

    return {
        'party': party,
        'form': form,
        'summary_rows': summary_rows or [],
        'problems': problems or [],
        'problems_truncated': problems_truncated,
        'document_b64': document_b64,
        'max_document_kib': (
            tournament_config_document.MAX_DOCUMENT_BYTES // 1024
        ),
    }


@blueprint.post('/for_party/<party_id>/import')
@permission_required('lan_tournament.create')
def import_config(party_id):
    """Check a configuration file, or create a draft tournament from it."""
    request.max_content_length = (
        tournament_config_service.IMPORT_MAX_REQUEST_BYTES
    )

    party = _get_party_or_404(party_id)

    try:
        formdata = _get_import_formdata()
    except RequestEntityTooLarge:
        problems, truncated = _import_over_budget_problems()
        body = import_form(
            party.id, problems=problems, problems_truncated=truncated
        )
        return body, 413

    action = formdata.get('action')
    if action not in ('check', 'import'):
        abort(400)

    form = TournamentImportForm(formdata)

    raw = _read_import_document(form)
    if raw is None:
        _add_field_error(
            form.config_file, gettext('Please choose a configuration file.')
        )
        return import_form(party.id, form)

    checked = tournament_config_service.check_import(raw)
    if checked.is_err():
        problems, truncated = _import_problems(checked.unwrap_err())
        return import_form(
            party.id, form, problems=problems, problems_truncated=truncated
        )
    check = checked.unwrap()

    summary_rows = _import_summary_rows(check.config)
    document_b64 = base64.b64encode(raw).decode('ascii')

    valid = form.validate()
    if not valid or action == 'check':
        return import_form(
            party.id,
            form,
            summary_rows=summary_rows,
            document_b64=document_b64,
        )

    creation_token, existing = _resolve_creation_token(
        form.submission_token.data, party
    )
    if creation_token != _parse_uuid(form.submission_token.data):
        form.submission_token.data = str(creation_token)
    if existing is not None:
        flash_notice(gettext('This tournament has already been created.'))
        return redirect_to('.view', tournament_id=existing.id)

    image_id: TournamentImageID | None = None
    upload = form.image.data
    if upload is not None and getattr(upload, 'filename', None):
        match tournament_image_service.store_uploaded_image(
            party.id, g.user.id, upload.stream, upload.filename
        ):
            case Ok(image):
                image_id = image.id
            case Err(message):
                _add_field_error(form.image, _translate_image_message(message))
                return import_form(
                    party.id,
                    form,
                    summary_rows=summary_rows,
                    document_b64=document_b64,
                )

    try:
        result = tournament_config_service.import_tournament_config(
            party.id,
            check,
            g.user.id,
            image_id=image_id,
            image_alt_text=form.image_alt_text.data or None,
            creation_token=creation_token,
        )
    except Exception:
        with suppress(Exception):
            tournament_repository.rollback_session()
            _discard_staged_image(image_id, party)
        raise

    if result.is_err():
        _discard_staged_image(image_id, party)
        error_message = result.unwrap_err()

        # A concurrent duplicate may trip another unique index first.
        existing = (
            tournament_service.find_tournament_by_creation_token(creation_token)
            if creation_token is not None
            else None
        )
        if existing is not None and existing.party_id == party.id:
            flash_notice(gettext('This tournament has already been created.'))
            return redirect_to('.view', tournament_id=existing.id)

        if error_message == tournament_service.IMAGE_UNAVAILABLE_ERROR:
            _add_field_error(form.image, gettext(error_message))
        else:
            form.form_errors.append(_translate_error(error_message))
        return import_form(
            party.id,
            form,
            summary_rows=summary_rows,
            document_b64=document_b64,
        )

    tournament, _event = result.unwrap()

    flash_success(
        gettext(
            'Tournament "%(name)s" has been imported as a draft.',
            name=tournament.name,
        )
    )

    return redirect_to('.view', tournament_id=tournament.id)


@blueprint.post('/tournaments/<tournament_id>/export')
@permission_required('lan_tournament.view')
def export_config(tournament_id):
    """Download the configuration document of a tournament."""
    tournament = _get_tournament_or_404(tournament_id)

    document = tournament_config_service.export_tournament_config(
        tournament, g.user.id
    )

    response = send_file(
        BytesIO(document),
        mimetype='application/json',
        as_attachment=True,
        download_name=tournament_config_document.export_filename(
            tournament.name, datetime.now(UTC).date()
        ),
    )
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response


def _read_import_document(form: TournamentImportForm) -> bytes | None:
    """Return the uploaded or carried document, `None` if there is none."""
    max_bytes = tournament_config_document.MAX_DOCUMENT_BYTES

    upload = form.config_file.data
    if upload is not None and getattr(upload, 'filename', None):
        return upload.stream.read(max_bytes + 1)

    carried = form.config_document.data
    if not carried or len(carried) > 4 * math.ceil((max_bytes + 1) / 3):
        return None

    try:
        return base64.b64decode(carried, validate=True)
    except ValueError:
        return None


def _get_import_formdata():
    """Return the request form with the files of the two file inputs."""
    formdata = request.form.copy()
    for name in ('config_file', 'image'):
        if name in request.files:
            formdata[name] = request.files[name]
    return formdata


def _create_form_labels() -> dict[str, str]:
    """Return the label of every field of the create form."""
    return {
        field.name: str(field.label.text) for field in TournamentCreateForm()
    }


def _import_problems(
    problems: list[tournament_config_document.DocumentProblem],
) -> tuple[list[tuple[str, str]], bool]:
    """Return label and message of the problems to show, and if cut off."""
    shown = problems[: tournament_config_document.MAX_REPORTED_PROBLEMS]
    labels = _create_form_labels()
    return (
        [
            (
                labels.get(problem.location, ''),
                _translate_validation_message(problem.message),
            )
            for problem in shown
        ],
        len(problems) > len(shown),
    )


def _import_over_budget_problems() -> tuple[list[tuple[str, str]], bool]:
    """Return the problem to show for a request above the body budget."""
    # Only step 2 carries an image; the query string needs no body parsing.
    if request.args.get('step') == 'import':
        label = str(TournamentImportForm().image.label.text)
        return [(label, _translate_image_too_large())], False

    too_large = tournament_config_document.DocumentProblem(
        '',
        ValidationMessage(
            tournament_config_document.DOCUMENT_TOO_LARGE_ERROR,
            (('max', tournament_config_document.MAX_DOCUMENT_BYTES // 1024),),
        ),
    )
    return _import_problems([too_large])


def _import_summary_rows(
    config: tournament_config_domain_service.TournamentConfig,
) -> list[tuple[str, str]]:
    """Return label and value of the main settings of a checked document."""
    labels = _create_form_labels()
    settings = config.settings
    none = '–'

    contestant_types: dict[ContestantType | None, str] = {
        ContestantType.SOLO: gettext('Solo'),
        ContestantType.TEAM: gettext('Team'),
    }
    game_format = settings.game_format
    elimination_mode = _build_elimination_mode_labels().get(
        settings.elimination_mode
    )

    rows = [
        (labels['name'], config.name),
        (labels['category'], str(config.category.label)),
        (labels['game'], config.game or none),
        (
            labels['contestant_type'],
            contestant_types.get(settings.contestant_type, none),
        ),
        (labels['game_format'], game_format.label if game_format else none),
        (labels['elimination_mode'], elimination_mode or none),
    ]

    for name in (
        'min_players',
        'max_players',
        'min_teams',
        'max_teams',
        'min_players_in_team',
        'max_players_in_team',
    ):
        value = getattr(settings, name)
        if value is not None:
            rows.append((labels[name], str(value)))

    rows.append(
        (
            labels['start_time'],
            format_datetime(to_user_timezone(config.start_time))
            if config.start_time
            else none,
        )
    )
    if settings.point_table:
        rows.append(
            (
                labels['point_table'],
                ', '.join(str(points) for points in settings.point_table),
            )
        )
    rows.append(
        (
            labels['playoff_enabled'],
            gettext('Yes')
            if settings.playoff_game_format is not None
            else gettext('No'),
        )
    )

    return rows


def _discard_staged_image(
    image_id: TournamentImageID | None, party: Party
) -> None:
    """Delete the image of a failed import; it is gone or in use if refused."""
    if image_id is None:
        return

    tournament_image_service.delete_staged_image(
        image_id, party_id=party.id, requester_id=g.user.id
    )


def _get_create_formdata():
    """Return the request form merged with its uploaded files."""
    formdata = request.form.copy()
    formdata.update(request.files)
    return formdata


def _build_create_form(formdata) -> TournamentCreateForm:
    form = TournamentCreateForm(formdata)
    form.set_contestant_type_choices()
    form.set_game_format_choices()
    form.set_elimination_mode_choices()
    form.set_score_ordering_choices()
    return form


def _create_wizard_urls(party: Party) -> dict[str, str]:
    return {
        'create': url_for('.create', party_id=party.id),
        'upload': url_for('.upload_create_image', party_id=party.id),
        'delete_template': url_for(
            '.delete_create_image', party_id=party.id, image_id='__ID__'
        ),
        'images': url_for('.list_create_images', party_id=party.id),
        'validate': url_for('.validate_create', party_id=party.id),
        'cancel': url_for('.index', party_id=party.id),
    }


def _parse_uuid(value) -> UUID | None:
    if not value:
        return None
    try:
        return UUID(str(value))
    except ValueError:
        return None


def _resolve_creation_token(
    raw_token, party: Party
) -> tuple[UUID | None, Tournament | None]:
    """Return the creation token for `party` and its existing tournament."""
    creation_token = _parse_uuid(raw_token)
    if creation_token is None:
        return None, None

    existing = tournament_service.find_tournament_by_creation_token(
        creation_token
    )
    if existing is not None and existing.party_id != party.id:
        # A token of another party's tournament is replaced by one
        # derived from it, so a repost stays idempotent in this party.
        creation_token = uuid5(creation_token, str(party.id))
        existing = tournament_service.find_tournament_by_creation_token(
            creation_token
        )
    if existing is not None and existing.party_id != party.id:
        existing = None

    return creation_token, existing


def _find_staged_image(raw_image_id, party: Party) -> TournamentImage | None:
    image_id = _parse_uuid(raw_image_id)
    if image_id is None:
        return None
    return tournament_image_service.find_attachable_image(
        TournamentImageID(image_id), party
    )


def _add_field_error(field, message: str) -> None:
    # `errors` is a tuple until the form has been validated.
    field.errors = [*field.errors, message]


def _translate_error(error: str | ValidationMessage) -> str:
    """Translate a service error, formatting its placeholders after it."""
    if isinstance(error, ValidationMessage):
        return _translate_validation_message(error)
    return gettext(error)


def _translate_validation_message(message: ValidationMessage) -> str:
    params = dict(message.params)
    if 'other' in params:
        # A field label, itself a msgid.
        params['other'] = gettext(params['other'])
    if message.msgid == tournament_domain_service.POINTS_TOO_HIGH_MSGID:
        params['max'] = format_decimal(params['max'])
    elif message.msgid == tournament_domain_service.POINTS_TOO_LOW_MSGID:
        params['min'] = format_decimal(params['min'])
    return gettext(message.msgid, **params)


def _apply_settings_errors(
    form, errors: dict[str, ValidationMessage]
) -> None:
    """Put settings errors on their fields; skip fields that have one."""
    for field_name, message in errors.items():
        translated = _translate_validation_message(message)
        field = getattr(form, field_name, None)
        if field is None:
            form.form_errors.append(translated)
        elif not field.errors:
            _add_field_error(field, translated)


def _parse_playoff_config(
    form, game_format, elimination_mode, on_invalid
) -> dict:
    """Return the six playoff kwargs a submission configures.

    All are `None` unless the switch is on and the format has a playoff
    phase, so stale values of a skipped step never reach the service.
    """
    config, errors = tournament_config_domain_service.playoff_config(
        enabled=bool(form.playoff_enabled.data),
        game_format=game_format,
        elimination_mode=elimination_mode,
        group_count=form.playoff_group_count.data,
        qualifiers_per_group=form.playoff_qualifiers_per_group.data,
        qualifier_count=form.playoff_qualifier_count.data,
        elimination_mode_name=form.playoff_elimination_mode.data,
        release_mode_name=form.playoff_release_mode.data,
    )
    for field_name, message in errors.items():
        on_invalid(
            getattr(form, field_name), _translate_validation_message(message)
        )
    return config


def _parse_create_submission(
    form: TournamentCreateForm, party: Party
) -> _CreateSubmission | None:
    """Parse and check a create POST; put errors on the form fields."""

    start_time_local = form.start_time.data
    start_time = (
        to_utc(start_time_local)
        if start_time_local and not form.start_time.errors
        else None
    )
    config_input = tournament_config_domain_service.TournamentConfigInput(
        name=form.name.data or '',
        category=form.category.data or None,
        game=form.game.data or None,
        description=form.description.data or None,
        ruleset=form.ruleset.data or None,
        start_time=start_time,
        contestant_type=form.contestant_type.data or None,
        game_format=form.game_format.data or None,
        elimination_mode=form.elimination_mode.data or None,
        score_ordering=form.score_ordering.data or None,
        min_players=form.min_players.data,
        max_players=form.max_players.data,
        min_teams=form.min_teams.data,
        max_teams=form.max_teams.data,
        min_players_in_team=form.min_players_in_team.data,
        max_players_in_team=form.max_players_in_team.data,
        point_table=form.point_table.data or None,
        advancement_count=form.advancement_count.data,
        group_size_min=form.group_size_min.data,
        group_size_max=form.group_size_max.data,
        points_carry_to_losers=bool(form.points_carry_to_losers.data),
        playoff_enabled=bool(form.playoff_enabled.data),
        playoff_group_count=form.playoff_group_count.data,
        playoff_qualifiers_per_group=form.playoff_qualifiers_per_group.data,
        playoff_qualifier_count=form.playoff_qualifier_count.data,
        playoff_elimination_mode=form.playoff_elimination_mode.data or None,
        playoff_release_mode=form.playoff_release_mode.data or None,
    )
    config = None
    match tournament_config_domain_service.normalize_config(config_input):
        case Ok(normalized):
            config = normalized
        case Err(config_errors):
            _apply_settings_errors(form, config_errors)

    image_url = form.image_url.data.strip() if form.image_url.data else None

    # `from_request_id` arrives from a hidden form field -- client
    # supplied, so re-load and re-verify party ownership rather than
    # trusting it outright.
    source_request = None
    if form.from_request_id.data:
        if not (
            g.user.has_permission('lan_tournament.request_decide')
            and g.user.has_permission('lan_tournament.request_view')
        ):
            # Consuming a request moves it to `tournament_created` and
            # appoints its proposer as orga -- a decision, not a view.
            # `request_view` is required too, in lockstep with
            # `create_form`'s prefill gate. Refuse before any lookup, so
            # nothing about the request leaks.
            _add_field_error(
                form.from_request_id,
                gettext(
                    'You are not allowed to create a tournament from a request.'
                ),
            )
        else:
            source_request = _find_request_for_party(
                form.from_request_id.data, party.id
            )
            if source_request is None:
                # Malformed, unknown, or belongs to another party -- never
                # silently fall through to an unlinked create.
                _add_field_error(
                    form.from_request_id,
                    gettext('Request is no longer in the expected state.'),
                )
            elif not _is_request_recreatable(source_request):
                # Decided by another admin, withdrawn, or still linked.
                # A UX fast path only: `create_tournament` re-runs the
                # check under a row lock.
                _add_field_error(
                    form.from_request_id,
                    gettext(
                        'Request #%(number)s is no longer accepted. '
                        'No tournament was created; your entries are kept.',
                        number=f'{source_request.number:04d}',
                    ),
                )

    image_id = None
    has_upload = getattr(form.image.data, 'filename', None)
    if form.image_id.data and not has_upload:
        parsed_image_id = _parse_uuid(form.image_id.data)
        image = (
            tournament_image_service.find_attachable_image(
                TournamentImageID(parsed_image_id), party
            )
            if parsed_image_id is not None
            else None
        )
        if image is None:
            _add_field_error(
                form.image_id,
                gettext(tournament_image_service.IMAGE_UNAVAILABLE_ERROR),
            )
        else:
            image_id = image.id
    image_alt_text = form.image_alt_text.data or None

    if (
        config is None
        or any(field.errors for field in form)
        or form.form_errors
    ):
        return None

    return _CreateSubmission(
        name=config.name,
        category=config.category,
        game=config.game,
        description=config.description,
        image_url=image_url,
        ruleset=config.ruleset,
        start_time=config.start_time,
        settings=config.settings,
        points_carry_to_losers=config.points_carry_to_losers,
        source_request=source_request,
        image_id=image_id,
        image_alt_text=image_alt_text,
        creation_token=_parse_uuid(form.submission_token.data),
    )


# --- Create wizard: image endpoints (JSON) ---

_IMAGE_LIST_MAX_PAGE = 10_000
_IMAGE_LIST_MAX_QUERY_LENGTH = 100
_IMAGE_DELETE_STATUS_BY_MSGID = {
    tournament_image_service.IMAGE_UNAVAILABLE_ERROR: 404,
    tournament_image_service.IMAGE_NOT_CREATOR_ERROR: 403,
    tournament_image_service.IMAGE_IN_USE_ERROR: 409,
}


@blueprint.post('/for_party/<party_id>/create/image')
@permission_required('lan_tournament.create')
def upload_create_image(party_id):
    """Store an uploaded tournament image; return its data as JSON."""
    request.max_content_length = tournament_image_service.MAX_REQUEST_BYTES

    party = _get_party_or_404(party_id)

    try:
        upload = request.files.get('image')
    except RequestEntityTooLarge:
        return _json_error(_translate_image_too_large(), 413)

    if upload is None or not upload.filename:
        return _json_error(gettext('No file selected.'), 400)

    match tournament_image_service.store_uploaded_image(
        party.id, g.user.id, upload.stream, upload.filename
    ):
        case Ok(image):
            return (
                jsonify(
                    image_id=str(image.id),
                    url=tournament_image_service.get_image_url_path(image),
                    filename=image.filename,
                    width=image.width,
                    height=image.height,
                    byte_size=image.byte_size,
                ),
                201,
            )
        case Err(message):
            if message.msgid == tournament_image_service.IMAGE_TYPE_ERROR:
                status = 415
            elif message.msgid == tournament_image_service.IMAGE_SIZE_ERROR:
                status = 413
            else:
                status = 400
            return _json_error(_translate_image_message(message), status)


@blueprint.delete('/for_party/<party_id>/create/image/<image_id>')
@permission_required('lan_tournament.create')
def delete_create_image(party_id, image_id):
    """Delete a staged image of the current user."""
    party = _get_party_or_404(party_id)

    try:
        parsed_image_id = TournamentImageID(UUID(image_id))
    except ValueError:
        return _json_error(
            gettext(tournament_image_service.IMAGE_UNAVAILABLE_ERROR), 404
        )

    match tournament_image_service.delete_staged_image(
        parsed_image_id, party_id=party.id, requester_id=g.user.id
    ):
        case Ok():
            return '', 204
        case Err(message):
            return _json_error(
                _translate_image_message(message),
                _IMAGE_DELETE_STATUS_BY_MSGID.get(message.msgid, 400),
            )


@blueprint.get('/for_party/<party_id>/images')
@permission_required('lan_tournament.create')
def list_create_images(party_id):
    """List the images the party may pick from, as JSON."""
    party = _get_party_or_404(party_id)

    scope = request.args.get('scope', 'party')
    if scope not in ('party', 'brand'):
        return _json_error(gettext('Invalid image scope.'), 400)

    try:
        page = int(request.args.get('page', '1'))
    except ValueError:
        page = 0
    if not 1 <= page <= _IMAGE_LIST_MAX_PAGE:
        return _json_error(gettext('Invalid page number.'), 400)

    filename_query = (
        request.args.get('q', '').strip()[:_IMAGE_LIST_MAX_QUERY_LENGTH] or None
    )

    picker_page = tournament_image_service.list_picker_images(
        party, scope=scope, filename_query=filename_query, page=page
    )

    return jsonify(
        items=[
            {
                'image_id': str(item.image.id),
                'url': tournament_image_service.get_image_url_path(item.image),
                'filename': item.image.filename,
                'width': item.image.width,
                'height': item.image.height,
                'byte_size': item.image.byte_size,
                'party_title': picker_page.party_titles.get(
                    item.image.party_id
                ),
                'used_by': list(item.used_by),
                'created_at': item.image.created_at.isoformat(),
            }
            for item in picker_page.items
        ],
        page=picker_page.page,
        has_next=picker_page.has_next,
    )


def _json_error(message: str, status: int):
    return jsonify(error=message), status


def _translate_image_too_large() -> str:
    return _translate_image_message(
        ValidationMessage(
            tournament_image_service.IMAGE_SIZE_ERROR,
            (('size', tournament_image_service.MAX_UPLOAD_BYTES + 1),),
        )
    )


def _translate_image_message(message: ValidationMessage) -> str:
    """Translate the message, formatting size and megapixel parameters."""
    params = dict(message.params)

    size = params.get('size')
    if isinstance(size, int):
        if size > tournament_image_service.MAX_UPLOAD_BYTES:
            # The service stops reading at the limit, so `size` is not the
            # real size; the request length is the closest known value.
            length = request.content_length
            size = (
                _format_megabytes(length)
                if length is not None and length >= size
                else gettext('more than 5 MB')
            )
        else:
            size = _format_megabytes(size)
        params['size'] = size

    mp = params.get('mp')
    if isinstance(mp, str):
        params['mp'] = format_decimal(float(mp), format='#,##0.0')

    return gettext(message.msgid, **params)


def _format_megabytes(size: int) -> str:
    return format_decimal(size / 1048576, format='#,##0.0') + ' MB'


# --- Tournament requests (party-wide admin queue + detail) ---

_PENDING_REQUEST_STATUSES = frozenset(
    {TournamentRequestStatus.submitted, TournamentRequestStatus.accepted}
)

# Only `submitted` requests count as open for the nav tab badge.
_OPEN_REQUEST_STATUSES = frozenset({TournamentRequestStatus.submitted})


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


@blueprint.app_template_global('lan_tournament_has_orga_assignments')
def _has_orga_assignments_for_nav(party_id) -> bool:
    if not g.user.has_permission('lan_tournament.view'):
        return False
    cache = request.environ.setdefault('byceps.lan_tournament_orga_nav', {})
    key = (party_id, g.user.id)
    if key not in cache:
        cache[key] = tournament_orga_service.has_orga_assignments_for_party(
            PartyID(party_id), g.user.id
        )
    return cache[key]


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
            PartyID(party_id), _OPEN_REQUEST_STATUSES
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

    # Submitted requests lead, then accepted ones; newest first in each.
    pending_requests = sorted(
        (r for r in visible_requests if r.status in _PENDING_REQUEST_STATUSES),
        key=lambda r: (
            r.status is not TournamentRequestStatus.submitted,
            -r.created_at.timestamp(),
        ),
    )
    done_requests = sorted(
        (
            r
            for r in visible_requests
            if r.status not in _PENDING_REQUEST_STATUSES
        ),
        key=lambda r: -r.created_at.timestamp(),
    )

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

    created_tournament = (
        tournament_service.find_tournament(
            tournament_request.created_tournament_id
        )
        if tournament_request.created_tournament_id
        else None
    )
    proposer_is_orga = (
        tournament_orga_service.is_orga_for_tournament(
            tournament_request.proposer_id, created_tournament.id
        )
        if created_tournament
        else False
    )

    user_ids = {tournament_request.proposer_id}
    if tournament_request.decided_by_id:
        user_ids.add(tournament_request.decided_by_id)
    initiator_ids = {
        entry.initiator_id for entry in history if entry.initiator_id
    }
    user_ids.update(initiator_ids)
    users_by_id = user_service.get_users_indexed_by_id(user_ids)
    seats_by_user_id = build_seat_lookup(initiator_ids, party.id)

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
        'seats_by_user_id': seats_by_user_id,
        'reject_form': reject_form,
        'elimination_mode_labels': _build_elimination_mode_labels(),
        'created_tournament': created_tournament,
        'proposer_is_orga': proposer_is_orga,
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


_REJECT_REASON_ERRORS = frozenset(
    {
        'A reason is required to reject a request.',
        'The reason must not contain control characters.',
        'The reason must not exceed 2000 characters.',
    }
)


@blueprint.post('/requests/<request_id>/reject')
@permission_required('lan_tournament.request_decide')
def reject_request(request_id):
    """Reject a tournament request."""
    if not g.user.has_permission('lan_tournament.request_view'):
        abort(403)

    tournament_request = _get_request_or_404(request_id)

    form = TournamentRequestRejectForm(request.form)
    if not form.validate():
        # Show one error; a blank reason gets the service's required message.
        if not (form.reason.data or '').strip():
            form.reason.errors = [
                gettext('A reason is required to reject a request.')
            ]
        else:
            form.reason.errors = list(form.reason.errors[:1])
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
        case Err(error_message) if error_message in _REJECT_REASON_ERRORS:
            # Whitespace-only input passes `validate()`; `errors` is a tuple.
            form.reason.errors = [
                *form.reason.errors,
                gettext(error_message),
            ]
            return view_request(request_id, erroneous_reject_form=form)
        case Err(error_message):
            flash_error(gettext(error_message))
            return redirect_to(
                '.view_request', request_id=tournament_request.id
            )


@blueprint.get('/requests/<request_id>/reject')
@permission_required('lan_tournament.request_decide')
def reject_request_redirect(request_id):
    """Redirect a GET on the reject URL to the request detail."""
    if not g.user.has_permission('lan_tournament.request_view'):
        abort(403)

    tournament_request = _get_request_or_404(request_id)

    return redirect_to('.view_request', request_id=tournament_request.id)


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


def _find_refused_request(raw_request_id, party) -> TournamentRequest | None:
    """Return the request behind a refused link, if the user may see it."""
    if not (
        g.user.has_permission('lan_tournament.request_view')
        and g.user.has_permission('lan_tournament.request_decide')
    ):
        return None

    found_request = _find_request_for_party(raw_request_id, party.id)
    if found_request is None or _is_request_recreatable(found_request):
        return None

    return found_request


def _build_refusal(refused_request: TournamentRequest) -> dict:
    proposer_name = user_service.find_screen_name(refused_request.proposer_id)
    decider_name = (
        user_service.find_screen_name(refused_request.decided_by_id)
        if refused_request.decided_by_id
        else None
    )
    return build_request_refusal(
        refused_request,
        proposer_name=proposer_name,
        decider_name=decider_name,
        view_url=url_for('.view_request', request_id=refused_request.id),
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

    Contestant type is derived from `team_size` (`analyze_field_gap`'s
    12/6 split): 1 means solo, so the limit maps to `max_players`;
    anything higher means team, so the limit maps to `max_teams` and
    the team size fills both `min_players_in_team` and
    `max_players_in_team`. `create`'s own clearing of the unselected
    type's fields (its contestant-type branch) is what keeps the
    other type's fields from leaking through on submit -- this
    function only ever sets one side.
    """
    form.from_request_id.data = str(tournament_request.id)
    form.category.data = TournamentCategory.USER_ORGANIZED.value
    form.name.data = tournament_request.name
    form.game.data = tournament_request.game
    form.description.data = tournament_request.description
    form.ruleset.data = tournament_request.special_rules
    form.game_format.data = tournament_request.game_format.name
    form.elimination_mode.data = tournament_request.elimination_mode.name
    form.start_time.data = to_user_timezone(
        tournament_request.preferred_start_time
    )
    if tournament_request.team_size == 1:
        form.contestant_type.data = ContestantType.SOLO.name
        form.max_players.data = tournament_request.participant_limit
    else:
        form.contestant_type.data = ContestantType.TEAM.name
        form.max_teams.data = tournament_request.participant_limit
        form.min_players_in_team.data = tournament_request.team_size
        form.max_players_in_team.data = tournament_request.team_size


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
    """Show the admin form to edit a submitted or accepted request."""
    if not g.user.has_permission('lan_tournament.request_view'):
        abort(403)

    tournament_request = _get_request_or_404(request_id)

    if not tournament_request.is_editable_by_admin:
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

    users_by_id = user_service.get_users_indexed_by_id(
        {tournament_request.proposer_id}
    )
    proposer = users_by_id.get(tournament_request.proposer_id)
    if proposer is None:
        proposer_name = str(tournament_request.proposer_id)
    else:
        proposer_name = proposer.screen_name or gettext('Deleted user')

    return {
        'party': party,
        'tournament_request': tournament_request,
        'form': form,
        'party_capacity': party.max_ticket_quantity,
        'users_by_id': users_by_id,
        'proposer_name': proposer_name,
    }


@blueprint.post('/requests/<request_id>/update')
@permission_required('lan_tournament.request_decide')
def update_request(request_id):
    """Update a submitted or accepted tournament request (admin edit)."""
    if not g.user.has_permission('lan_tournament.request_view'):
        abort(403)

    tournament_request = _get_request_or_404(request_id)

    if not tournament_request.is_editable_by_admin:
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
        data['category'] = tournament.category.value
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
        data['playoff_enabled'] = tournament.has_playoffs
        data['playoff_elimination_mode'] = (
            tournament.playoff_elimination_mode.name
            if tournament.playoff_elimination_mode
            else ''
        )
        data['playoff_release_mode'] = (
            tournament.playoff_release_mode.name
            if tournament.playoff_release_mode
            else ''
        )
        form = TournamentUpdateForm(data=data)

    form.set_contestant_type_choices()
    form.set_game_format_choices()
    form.set_elimination_mode_choices()
    form.set_score_ordering_choices()

    locked_playoff = tournament_service.locked_playoff_fields(tournament)
    governed = tournament_service.playoff_fields(tournament)
    return {
        'party': party,
        'tournament': tournament,
        'form': form,
        'is_locked': is_locked,
        'locked_playoff_fields': locked_playoff,
        'ffa_locked': (
            'point_table' in locked_playoff
            if 'point_table' in governed
            else is_locked
        ),
    }


_UNCHANGED_COUNT_FIELDS = (
    'min_players',
    'max_players',
    'min_teams',
    'max_teams',
    'min_players_in_team',
    'max_players_in_team',
    'group_size_min',
    'group_size_max',
    'advancement_count',
)

# (min field, max field); the max field carries the pair's error.
_MIN_MAX_PAIRS = (
    ('min_players', 'max_players'),
    ('min_teams', 'max_teams'),
    ('min_players_in_team', 'max_players_in_team'),
)


def _parse_point_table(raw: str | None) -> list[int] | None:
    """Parse a comma-separated table; `None` if empty, `ValueError` if bad."""
    values = [int(v.strip()) for v in (raw or '').split(',') if v.strip()]
    return values or None


def _is_blank(field) -> bool:
    return not any((raw or '').strip() for raw in field.raw_data or [])


def _is_unchanged(form, name: str, tournament) -> bool:
    """Tell if the posted value of a field equals the stored one."""
    field = form[name]
    if name == 'point_table':
        try:
            return _parse_point_table(field.data) == tournament.point_table
        except ValueError:
            return False
    if name == 'start_time':
        stored = tournament.start_time
        if field.data is None:
            return stored is None and _is_blank(field)
        if stored is None:
            return False
        stored_local = to_user_timezone(stored).replace(
            tzinfo=None, second=0, microsecond=0
        )
        return field.data.replace(second=0, microsecond=0) == stored_local
    stored = getattr(tournament, name)
    if field.data is None:
        # A value that failed to parse is `None` as well.
        return stored is None and _is_blank(field)
    return field.data == stored


def discard_errors_on_unchanged_fields(form, tournament) -> None:
    """Drop errors on fields still holding their stored value.

    Rules added after a tournament was created must not lock out an
    edit that leaves the offending value alone. A min/max pair error
    stays while either field of the pair changed.
    """
    names = (*_UNCHANGED_COUNT_FIELDS, 'point_table', 'start_time')
    unchanged = {
        name for name in names if _is_unchanged(form, name, tournament)
    }

    max_to_min = {max_name: min_name for min_name, max_name in _MIN_MAX_PAIRS}
    for name in unchanged:
        field = form[name]
        if not field.errors:
            continue
        min_name = max_to_min.get(name)
        if min_name is not None and min_name not in unchanged:
            cross = min_above_max_error(form, field, min_name)
            field.errors = [cross] if cross is not None else []
        else:
            field.errors = []


_FFA_FORM_FIELDS = (
    'point_table',
    'group_size_min',
    'group_size_max',
    'advancement_count',
    'points_carry_to_losers',
)


def _inject_stored_fields(formdata, tournament, names) -> None:
    """Put the stored values of these fields into the form data."""
    for field in names:
        if field == 'playoff_game_format':
            name, value = 'playoff_enabled', tournament.has_playoffs
        else:
            name, value = field, getattr(tournament, field)
        if name in ('playoff_enabled', 'points_carry_to_losers'):
            if value:
                formdata[name] = 'y'
            else:
                formdata.pop(name, None)
        elif name == 'point_table':
            formdata[name] = (
                ', '.join(str(v) for v in value) if value is not None else ''
            )
        elif isinstance(value, Enum):
            formdata[name] = value.name
        else:
            formdata[name] = str(value) if value is not None else ''


def _update_form_is_valid(form, tournament) -> bool:
    """Validate the edit form, ignoring unchanged stored values."""
    form.validate()
    discard_errors_on_unchanged_fields(form, tournament)
    return not form.errors


@blueprint.post('/tournaments/<tournament_id>')
@permission_required('lan_tournament.update')
def update(tournament_id):
    """Update the tournament."""
    tournament = _get_tournament_or_404(tournament_id)

    is_locked = tournament.tournament_status in EDIT_LOCKED_STATUSES
    locked_playoff = tournament_service.locked_playoff_fields(tournament)
    governed = tournament_service.playoff_fields(tournament)

    # Disabled fields are not submitted by browsers.  Inject the
    # stored values so WTForms validation passes normally.
    formdata = (
        request.form.copy() if is_locked or locked_playoff else request.form
    )
    if is_locked:
        formdata['name'] = tournament.name
        formdata['category'] = tournament.category.value
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
        # A highscore playoff phase takes its FFA fields from the
        # playoff rules, which lock them only after the release.
        if 'point_table' not in governed:
            _inject_stored_fields(formdata, tournament, _FFA_FORM_FIELDS)
    _inject_stored_fields(formdata, tournament, locked_playoff)

    form = TournamentUpdateForm(formdata)
    form.set_contestant_type_choices()
    form.set_game_format_choices()
    form.set_elimination_mode_choices()
    form.set_score_ordering_choices()

    if not _update_form_is_valid(form, tournament):
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
    # A blank `contestant_type` still derives to TEAM/SOLO from team
    # size at the service layer, so branch on that same derived type
    # here too -- otherwise a blank type deriving to TEAM keeps
    # `max_players`, which then caps participant joins meant for solo.
    effective_contestant_type = (
        tournament_domain_service.derive_contestant_type(
            contestant_type, max_players_in_team, min_players_in_team
        )
    )
    if effective_contestant_type == ContestantType.SOLO:
        min_teams = None
        max_teams = None
        min_players_in_team = None
        max_players_in_team = None
    elif effective_contestant_type == ContestantType.TEAM:
        min_players = None
        max_players = None

    # Clear score ordering for non-highscore game formats.
    if game_format != GameFormat.HIGHSCORE:
        score_ordering = None

    playoff_errors: list[str] = []
    playoff = _parse_playoff_config(
        form,
        game_format,
        elimination_mode,
        lambda field, message: playoff_errors.append(message),
    )
    if playoff_errors:
        for message in playoff_errors:
            flash_error(message)
        return update_form(tournament.id, form)

    # Parse FFA fields. A highscore playoff phase is a Free-for-All phase
    # and carries its settings in the same fields.
    point_table = None
    advancement_count = None
    group_size_min = None
    group_size_max = None
    points_carry_to_losers = None
    if game_format == GameFormat.FREE_FOR_ALL:
        ffa_mode = elimination_mode
    else:
        ffa_mode = playoff['playoff_elimination_mode']
    if (
        game_format == GameFormat.FREE_FOR_ALL
        or playoff['playoff_game_format'] == GameFormat.FREE_FOR_ALL
    ):
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
            table_problem = None
            if point_table != tournament.point_table:
                table_problem = tournament_domain_service.check_point_count(
                    point_table
                ) or tournament_domain_service.check_point_values(point_table)
            if table_problem is not None:
                flash_error(_translate_validation_message(table_problem))
                return update_form(tournament.id, form)
        advancement_count = form.advancement_count.data
        group_size_min = form.group_size_min.data
        group_size_max = form.group_size_max.data
        if ffa_mode == EliminationMode.DOUBLE_ELIMINATION:
            points_carry_to_losers = form.points_carry_to_losers.data

    playoff_settings = tournament_domain_service.TournamentSettings(
        contestant_type=effective_contestant_type,
        game_format=game_format,
        elimination_mode=elimination_mode,
        score_ordering=score_ordering,
        min_players=min_players,
        max_players=max_players,
        min_teams=min_teams,
        max_teams=max_teams,
        min_players_in_team=min_players_in_team,
        max_players_in_team=max_players_in_team,
        point_table=point_table,
        group_size_min=group_size_min,
        group_size_max=group_size_max,
        advancement_count=advancement_count,
        **playoff,
    )
    match tournament_domain_service.validate_playoff_settings(playoff_settings):
        case Err(playoff_errors_by_field):
            _apply_settings_errors(form, playoff_errors_by_field)
            for message in playoff_errors_by_field.values():
                flash_error(_translate_validation_message(message))
            return update_form(tournament.id, form)

    result = tournament_service.update_tournament(
        tournament.id,
        name=name,
        category=TournamentCategory(form.category.data),
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
        **playoff,
        initiator_id=g.user.id,
    )
    if result.is_err():
        flash_error(_translate_error(result.unwrap_err()))
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
    """Start the tournament; a changed board needs a confirmation."""
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
    return _change_status(
        tournament_id, TournamentStatus.ONGOING, allow_completed_reopen=True
    )


def _change_status(
    tournament_id,
    new_status: TournamentStatus,
    *,
    allow_completed_reopen: bool = False,
):
    """Change the tournament status."""
    tournament = _get_tournament_or_404(tournament_id)

    match tournament_service.change_status(
        tournament.id,
        new_status,
        g.user.id,
        confirm_generated_layout=bool(request.form.get(START_CONFIRM_FIELD)),
        allow_completed_reopen=allow_completed_reopen,
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
    """Send the orga to the seeding; generate nothing."""
    tournament = _get_tournament_or_404(tournament_id)

    return redirect_to('.seeding', tournament_id=tournament.id)


@blueprint.get('/tournaments/<tournament_id>/seeding')
@permission_required('lan_tournament.administrate')
@templated('admin/lan_tournament/seeding')
def seeding(tournament_id):
    """Show the seeding board of the tournament."""
    tournament = _get_tournament_or_404(tournament_id)
    party = party_service.get_party(tournament.party_id)
    target = request.args.get(
        'target', tournament_seeding_service.INITIAL_TARGET
    )

    match tournament_seeding_service.get_board(
        tournament.id, target, initiator_id=g.user.id
    ):
        case Err(tournament_seeding_service.ERR_NOT_OPEN_YET) if (
            target == tournament_seeding_service.INITIAL_TARGET
        ):
            board = None
        case Err(error_message):
            flash_error(gettext(error_message))
            return redirect_to('.view', tournament_id=tournament.id)
        case Ok(board):
            pass

    return {
        'party': party,
        'tournament': tournament,
        'board': seeding_board_payload(board) if board is not None else None,
        **seeding_audit_context(tournament.id, contestant_names(tournament.id)),
    }


@blueprint.post('/tournaments/<tournament_id>/seeding/actions')
@permission_required('lan_tournament.administrate')
def seeding_action(tournament_id):
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

    return redirect_to('.seeding', tournament_id=tournament.id, target=target)


@blueprint.post('/tournaments/<tournament_id>/seeding/generate')
@permission_required('lan_tournament.administrate')
def seeding_generate(tournament_id):
    """Generate the bracket, groups or lobbies from the seeding."""
    tournament = _get_tournament_or_404(tournament_id)
    target = request.form.get(
        'target', tournament_seeding_service.INITIAL_TARGET
    )

    version = parse_int(request.form.get('version'))
    if version is None:
        flash_error(gettext(tournament_seeding_service.ERR_INVALID_CHANGE))
        return redirect_to(
            '.seeding', tournament_id=tournament.id, target=target
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
            flash_success(generation_flash(tournament, target, match_count))
        case Err(error_message):
            flash_error(gettext(error_message))

    return redirect_to('.seeding', tournament_id=tournament.id, target=target)


def _seeding_error(tournament, target, error_message, status, json_wanted):
    message = gettext(error_message)
    if json_wanted:
        return _json_error(message, status)
    flash_error(message)
    return redirect_to('.seeding', tournament_id=tournament.id, target=target)


def _qualification_payload(tournament, state, names):
    decisions = tournament_qualification_repository.get_decisions_for_tournament(
        tournament.id
    )
    user_ids = {b.decided_by for d in decisions.values() for b in d.blocks}
    if state.released_by is not None:
        user_ids.add(state.released_by)
    return serialize_qualification(
        state,
        names,
        qualification_strings(),
        tournament=tournament,
        decisions=decisions,
        users=user_service.get_users_indexed_by_id(user_ids),
        submitted_at=leaderboard_submission_times(tournament.id, state),
    )


@blueprint.get('/tournaments/<tournament_id>/qualification')
@permission_required('lan_tournament.administrate')
@templated('admin/lan_tournament/qualification')
def qualification(tournament_id):
    """Show who qualifies for the playoffs, the ties and the release."""
    tournament = _get_tournament_or_404(tournament_id)
    party = party_service.get_party(tournament.party_id)

    match tournament_qualification_service.get_qualification(tournament.id):
        case Err(error_message):
            flash_error(gettext(error_message))
            return redirect_to('.view', tournament_id=tournament.id)
        case Ok(state):
            pass

    names = contestant_names(tournament.id)
    board = playoff_board(state)

    return {
        'party': party,
        'tournament': tournament,
        'qualification': _qualification_payload(tournament, state, names),
        'playoff_version': (
            board.version
            if board is not None and playoff_release_open(tournament, state)
            else None
        ),
        'playoff_board': (
            seeding_board_payload(board) if board is not None else None
        ),
        **seeding_audit_context(tournament.id, names),
        'js_strings': qualification_js_strings(),
    }


@blueprint.post('/tournaments/<tournament_id>/qualification/draft')
@permission_required('lan_tournament.administrate')
def qualification_draft_action(tournament_id):
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

    return redirect_to('.qualification', tournament_id=tournament.id)


def _qualification_draft_error(tournament, error_message, status, json_wanted):
    message = gettext(error_message)
    if json_wanted:
        return _json_error(message, status)
    flash_error(message)
    return redirect_to('.qualification', tournament_id=tournament.id)


def _qualification_outcome(
    tournament, result, success_message, json_wanted, *, back='.qualification'
):
    """Answer a qualification action: JSON state, or flash and redirect."""
    match result:
        case Err(error_message):
            status = seeding_error_status(error_message)
            if json_wanted:
                return _json_error(gettext(error_message), status)
            flash_error(gettext(error_message))
        case Ok(_):
            if json_wanted:
                state = tournament_qualification_service.get_qualification(
                    tournament.id
                )
                payload = (
                    _qualification_payload(
                        _get_tournament_or_404(tournament.id),
                        state.unwrap(),
                        contestant_names(tournament.id),
                    )
                    if state.is_ok()
                    else None
                )
                return jsonify(qualification=payload)
            flash_success(success_message)
    return redirect_to(back, tournament_id=tournament.id)


_FFA_DECISION_BACK_ENDPOINTS = {
    'bracket': '.bracket',
    'ffa_standings': '.ffa_standings',
}


def _qualification_back(scope, requested):
    """Return the endpoint a decision returns to, from a whitelist."""
    if not scope.startswith('ffa:'):
        return '.qualification'
    return _FFA_DECISION_BACK_ENDPOINTS.get(requested, '.bracket')


@blueprint.post('/tournaments/<tournament_id>/qualification/decisions')
@permission_required('lan_tournament.administrate')
def qualification_decide(tournament_id):
    """Save or withdraw an orga decision on a tie."""
    tournament = _get_tournament_or_404(tournament_id)
    json_wanted = wants_json(request)
    scope = request.form.get('scope', '').strip()
    action = request.form.get('action', '')
    back = _qualification_back(scope, request.form.get('back', ''))

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

    return _qualification_outcome(
        tournament, result, message, json_wanted, back=back
    )


@blueprint.post('/tournaments/<tournament_id>/qualification/draft/create')
@permission_required('lan_tournament.administrate')
def qualification_draft_create(tournament_id):
    """Create the playoff draft once the qualification is ready."""
    tournament = _get_tournament_or_404(tournament_id)

    result = tournament_seeding_service.ensure_playoff_draft(
        tournament.id, g.user.id
    )

    return _qualification_outcome(
        tournament,
        result,
        gettext('Playoff draft created.'),
        wants_json(request),
    )


@blueprint.post('/tournaments/<tournament_id>/qualification/release')
@permission_required('lan_tournament.administrate')
def qualification_release(tournament_id):
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

    return _qualification_outcome(tournament, result, message, json_wanted)


@blueprint.post('/tournaments/<tournament_id>/qualification/unrelease')
@permission_required('lan_tournament.administrate')
def qualification_unrelease(tournament_id):
    """Take the release of the playoffs back, with a reason."""
    tournament = _get_tournament_or_404(tournament_id)

    result = tournament_qualification_service.unrelease_playoffs(
        tournament.id,
        reason=request.form.get('reason', ''),
        initiator_id=g.user.id,
    )

    return _qualification_outcome(
        tournament,
        result,
        gettext('Release undone.'),
        wants_json(request),
    )


@blueprint.post('/tournaments/<tournament_id>/leaderboard/close')
@permission_required('lan_tournament.administrate')
def leaderboard_close(tournament_id):
    """End the score phase of a highscore tournament."""
    tournament = _get_tournament_or_404(tournament_id)

    result = tournament_score_service.close_leaderboard(
        tournament.id, initiator_id=g.user.id
    )

    return _qualification_outcome(
        tournament,
        result,
        gettext('Qualification closed.'),
        wants_json(request),
    )


@blueprint.post('/tournaments/<tournament_id>/leaderboard/reopen')
@permission_required('lan_tournament.administrate')
def leaderboard_reopen(tournament_id):
    """Reopen the score phase of a highscore tournament."""
    tournament = _get_tournament_or_404(tournament_id)
    result = tournament_score_service.reopen_leaderboard(
        tournament.id,
        reason=request.form.get('reason', ''),
        initiator_id=g.user.id,
    )
    return _qualification_outcome(
        tournament,
        result,
        gettext('Qualification reopened.'),
        wants_json(request),
    )


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
        team.id, member_user_id, initiator_id=g.user.id
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

    try:
        participant_uuid = TournamentParticipantID(UUID(participant_id))
    except ValueError:
        abort(404)

    result = tournament_participant_service.admin_remove_participant(
        tournament.id, participant_uuid, initiator=g.user
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


@blueprint.get('/for_party/<party_id>/maintenance')
@permission_required('lan_tournament.maintain')
@templated
def maintenance(party_id):
    """Show the maintenance actions of the party."""
    party = _get_party_or_404(party_id)

    return {
        'party': party,
        'rows': _maintenance_rows(party),
        'thresholds': _dashboard_thresholds_card(party),
    }


def _maintenance_rows(party) -> list[dict]:
    now = datetime.now(UTC)
    rows = []
    for action in tournament_maintenance_service.get_actions():
        if not g.user.has_permission(action.permission):
            continue

        summary = action.summarize(party.id, now)
        finding, kept_finding = _maintenance_finding(
            action.id, summary, format_file_size(summary.byte_size)
        )
        rows.append(
            {
                'action': action,
                'texts': _maintenance_texts(action.id),
                'summary': summary,
                'finding': finding,
                'kept_finding': kept_finding,
            }
        )

    return rows


@blueprint.get('/for_party/<party_id>/maintenance/<action_id>')
@permission_required('lan_tournament.maintain')
@templated
def maintenance_preview(party_id, action_id):
    """Show what a maintenance action would delete."""
    party = _get_party_or_404(party_id)
    action = _get_maintenance_action_or_404(action_id)

    now = datetime.now(UTC)
    preview = action.preview(party.id, now)

    creator_ids = {
        item.creator_id
        for item in [*preview.items, *preview.kept]
        if item.creator_id
    }
    users_by_id = user_service.get_users_indexed_by_id(creator_ids)

    def to_row(item):
        creator = users_by_id.get(item.creator_id) if item.creator_id else None
        return {
            'item': item,
            'size': format_file_size(item.byte_size),
            'age': format_timedelta(item.created_at - now, add_direction=True),
            'created_at': (
                f'{format_date(item.created_at, format="medium")},'
                f' {format_time(item.created_at, format="short")}'
            ),
            'uploader': creator.screen_name if creator else None,
        }

    return {
        'party': party,
        'action': action,
        'texts': {
            **_maintenance_texts(action.id),
            **_maintenance_preview_texts(action.id, party.title),
        },
        'rows': [to_row(item) for item in preview.items],
        'kept_rows': [to_row(item) for item in preview.kept],
        'more_count': preview.more_count,
        'total_count': len(preview.items) + preview.more_count,
        'locale': str(get_locale() or 'en').replace('_', '-'),
    }


@blueprint.post('/for_party/<party_id>/maintenance/<action_id>')
@permission_required('lan_tournament.maintain')
def run_maintenance_action(party_id, action_id):
    """Delete the selected items of a maintenance action."""
    party = _get_party_or_404(party_id)
    action = _get_maintenance_action_or_404(action_id)

    keys = request.form.getlist('key')
    if not keys:
        flash_notice(gettext('Nothing selected.'))
        return redirect_to(
            '.maintenance_preview', party_id=party.id, action_id=action.id
        )

    report = action.execute(party.id, keys, g.user.id, datetime.now(UTC))

    _flash_maintenance_report(action.id, report)

    return redirect_to('.maintenance', party_id=party.id)


_THRESHOLD_ACTIONS = frozenset({'save', 'reset'})
_THRESHOLD_ECHO_LENGTH = 64
_THRESHOLD_MAX_DIGITS = 9
_THRESHOLD_TIME_FORMAT = '%Y-%m-%dT%H:%M:%S.%f'
_THRESHOLD_TIME_LENGTH = 26


@blueprint.post('/for_party/<party_id>/maintenance/dashboard-thresholds')
@permission_required('lan_tournament.maintain')
def update_dashboard_thresholds(party_id):
    """Save or reset the dashboard thresholds of the party.

    The party comes from the URL. Every field is untrusted, the hidden
    version included: one that does not parse is stale, never an error page.
    """
    party = _get_party_or_404(party_id)
    submitted = _submitted_thresholds()

    csrf_result = validate_dashboard_csrf(
        g.user, _single_form_value('csrf_token')
    )
    if csrf_result.is_err():
        return _thresholds_page(
            party,
            submitted,
            403,
            notice=_threshold_notice(
                TRANSPORT_ERROR_CSRF_INVALID, str(CSRF_INVALID_NOTICE)
            ),
        )

    action = _single_form_value('action')
    if action not in _THRESHOLD_ACTIONS:
        return _thresholds_page(
            party,
            submitted,
            422,
            notice=_threshold_notice(
                TRANSPORT_ERROR_INVALID, gettext('Invalid form data.')
            ),
        )

    version = _parse_threshold_version(
        _single_form_value('expected_revision'),
        _single_form_value('expected_updated_at'),
    )
    if version is None:
        return _thresholds_stale_page(party, submitted)
    expected_revision, expected_updated_at = version

    if action == 'reset':
        result = reset_party_thresholds(
            party.id,
            expected_revision=expected_revision,
            expected_updated_at=expected_updated_at,
            initiator_id=g.user.id,
        )
        done_text = gettext(
            'The dashboard thresholds were reset to the default.'
        )
    else:
        yellow = _parse_threshold_minutes(_single_form_value('yellow_minutes'))
        red = _parse_threshold_minutes(_single_form_value('red_minutes'))
        if yellow is None or red is None:
            errors = {}
            if yellow is None:
                errors['yellow'] = gettext(INVALID_YELLOW_MINUTES_ERROR)
            if red is None:
                errors['red'] = gettext(INVALID_RED_MINUTES_ERROR)
            return _thresholds_page(party, submitted, 422, errors=errors)

        result = set_party_thresholds(
            party.id,
            yellow_minutes=yellow,
            red_minutes=red,
            expected_revision=expected_revision,
            expected_updated_at=expected_updated_at,
            initiator_id=g.user.id,
        )
        done_text = gettext('The dashboard thresholds were saved.')

    if result.is_err():
        return _thresholds_refusal(party, submitted, result.unwrap_err())

    flash_success(done_text)

    return redirect(url_for('.maintenance', party_id=party.id), code=303)


def _single_form_value(name: str) -> str | None:
    """Return the value of a field that was sent exactly once."""
    values = request.form.getlist(name)
    return values[0] if len(values) == 1 else None


def _submitted_thresholds() -> dict[str, str]:
    """Return what the card sent, bounded, to show it again."""
    return {
        key: request.form.get(name, '')[:_THRESHOLD_ECHO_LENGTH]
        for key, name in (
            ('yellow', 'yellow_minutes'),
            ('red', 'red_minutes'),
            ('revision', 'expected_revision'),
            ('updated_at', 'expected_updated_at'),
        )
    }


def _parse_threshold_minutes(raw: str | None) -> int | None:
    """Accept only a short run of ASCII digits."""
    text = (raw or '').strip()
    if (
        1 <= len(text) <= _THRESHOLD_MAX_DIGITS
        and text.isascii()
        and text.isdigit()
    ):
        return int(text)

    return None


def _parse_threshold_version(
    revision_raw: str | None, updated_at_raw: str | None
) -> tuple[int, datetime | None] | None:
    """Return the version the card was rendered from, or `None` if unusable.

    The time is the exact shape the card renders: naive UTC with six
    fractional digits, empty while no override exists.
    """
    try:
        revision = parse_dashboard_revision(revision_raw)
    except ValueError:
        return None

    if updated_at_raw is None:
        return None

    if updated_at_raw == '':
        return revision, None

    if len(updated_at_raw) != _THRESHOLD_TIME_LENGTH:
        return None

    try:
        return revision, datetime.strptime(
            updated_at_raw, _THRESHOLD_TIME_FORMAT
        )
    except ValueError:
        return None


def _threshold_notice(code: str, text: str) -> dict[str, str]:
    return {'code': code, 'text': text}


def _thresholds_stale_page(party, submitted):
    return _thresholds_page(
        party,
        submitted,
        409,
        notice=_threshold_notice(
            TRANSPORT_ERROR_STALE,
            gettext(
                'The thresholds were changed in the meantime. Please reload.'
            ),
        ),
    )


def _thresholds_refusal(party, submitted, error: str):
    """Answer a save or reset that the service refused."""
    if error == THRESHOLDS_STALE_ERROR:
        return _thresholds_stale_page(party, submitted)

    errors = _threshold_field_errors(error, submitted)
    notice = (
        None
        if errors
        else _threshold_notice(TRANSPORT_ERROR_INVALID, gettext(error))
    )

    return _thresholds_page(party, submitted, 422, notice=notice, errors=errors)


def _threshold_field_errors(error: str, submitted) -> dict[str, str]:
    """Map the service's threshold errors to the field they belong to."""
    yellow = _parse_threshold_minutes(submitted['yellow']) or 0
    red = _parse_threshold_minutes(submitted['red']) or 0

    if error == INVALID_YELLOW_MINUTES_ERROR:
        if yellow > MAX_THRESHOLD_MINUTES:
            return {'yellow': gettext('At most 1440 minutes.')}
        return {'yellow': gettext('Yellow must be at least 1 minute.')}

    if error == INVALID_RED_MINUTES_ERROR and red > MAX_THRESHOLD_MINUTES:
        return {'red': gettext('At most 1440 minutes.')}

    if error in (INVALID_RED_MINUTES_ERROR, INVALID_THRESHOLD_ORDER_ERROR):
        return {'red': gettext('Red must be greater than yellow.')}

    return {}


def _thresholds_page(party, submitted, status, *, notice=None, errors=None):
    """Render the tab again, with what the card sent and why it was refused."""
    html = render_template(
        'admin/lan_tournament/maintenance.html',
        party=party,
        rows=_maintenance_rows(party),
        thresholds=_dashboard_thresholds_card(
            party, submitted=submitted, notice=notice, errors=errors
        ),
    )

    return make_response(html, status)


def _dashboard_thresholds_card(
    party, *, submitted=None, notice=None, errors=None
) -> dict:
    """Return the Wartung card of the dashboard thresholds.

    The values, the source line and the version come from one read of the
    override, so a save is compared with what the card says. A refused card
    shows what it sent, version included: sending it again stays refused
    until the page is reloaded.
    """
    deployment = get_dashboard_settings()
    if deployment.is_err():
        return {'unavailable': gettext(deployment.unwrap_err())}

    override = get_party_thresholds(party.id)
    if override is None:
        settings = deployment.unwrap()
        yellow, red = settings.yellow_minutes, settings.red_minutes
        source = 'deployment'
        source_line = gettext(
            'Installation default: %(yellow)d/%(red)d min',
            yellow=yellow,
            red=red,
        )
        revision, updated_at = '0', ''
    else:
        yellow, red = override.yellow_minutes, override.red_minutes
        source = 'party'
        actor = user_service.find_user(override.updated_by)
        source_line = gettext(
            'Set for this party by %(actor)s on %(date)s',
            actor=(actor.screen_name if actor else None)
            or gettext('Deleted orga'),
            date=format_date(override.updated_at, format='medium'),
        )
        revision = str(override.revision)
        updated_at = override.updated_at.isoformat(timespec='microseconds')

    sent = submitted or {
        'yellow': str(yellow),
        'red': str(red),
        'revision': revision,
        'updated_at': updated_at,
    }

    return {
        'unavailable': None,
        'action_url': url_for(
            '.update_dashboard_thresholds', party_id=party.id
        ),
        'csrf_token': get_dashboard_csrf_token(g.user),
        'source': source,
        'source_line': source_line,
        'yellow': sent['yellow'],
        'red': sent['red'],
        'revision': sent['revision'],
        'updated_at': sent['updated_at'],
        'notice': notice,
        'errors': errors or {},
    }


def _flash_maintenance_report(
    action_id: str, report: tournament_maintenance_service.CleanupReport
) -> None:
    """Flash what a maintenance action did."""
    size = format_file_size(report.byte_size)
    is_image_action = action_id == 'unused-images'

    if not report.deleted_count:
        flash_notice(gettext('Nothing was deleted.'))
    elif is_image_action:
        flash_success(
            ngettext(
                '%(count)s image deleted, %(size)s freed.',
                '%(count)s images deleted, %(size)s freed.',
                report.deleted_count,
                count=report.deleted_count,
                size=size,
            )
        )
    else:
        flash_success(
            ngettext(
                '%(count)s file deleted, %(size)s freed.',
                '%(count)s files deleted, %(size)s freed.',
                report.deleted_count,
                count=report.deleted_count,
                size=size,
            )
        )

    if is_image_action:
        if report.in_use_count:
            flash_notice(
                ngettext(
                    '%(count)s image was skipped because a tournament uses'
                    ' it now.',
                    '%(count)s images were skipped because a tournament uses'
                    ' them now.',
                    report.in_use_count,
                    count=report.in_use_count,
                )
            )
        other_skipped_count = report.skipped_count - report.in_use_count
        if other_skipped_count:
            flash_notice(
                ngettext(
                    '%(count)s image was skipped because it no longer'
                    ' qualifies.',
                    '%(count)s images were skipped because they no longer'
                    ' qualify.',
                    other_skipped_count,
                    count=other_skipped_count,
                )
            )
    elif report.skipped_count:
        flash_notice(
            ngettext(
                '%(count)s file was skipped because it no longer'
                ' qualifies.',
                '%(count)s files were skipped because they no longer'
                ' qualify.',
                report.skipped_count,
                count=report.skipped_count,
            )
        )

    if report.failed_file_count:
        if is_image_action:
            flash_error(
                ngettext(
                    '%(count)s file could not be deleted from disk. It now'
                    ' appears under "Orphaned image files".',
                    '%(count)s files could not be deleted from disk. They now'
                    ' appear under "Orphaned image files".',
                    report.failed_file_count,
                    count=report.failed_file_count,
                )
            )
        else:
            flash_error(
                ngettext(
                    '%(count)s file could not be deleted from disk.',
                    '%(count)s files could not be deleted from disk.',
                    report.failed_file_count,
                    count=report.failed_file_count,
                )
            )


def _get_maintenance_action_or_404(action_id):
    action = tournament_maintenance_service.find_action(action_id)

    if action is None:
        abort(404)

    if not g.user.has_permission(action.permission):
        abort(403)

    return action


def _maintenance_preview_texts(
    action_id: str, party_title: str
) -> dict[str, str]:
    """Return the texts of the preview page of the maintenance action."""
    texts = {
        'back': gettext('‹ Maintenance'),
        'select_all': gettext('Select all'),
        'cancel': gettext('Cancel'),
        'more_hint': gettext('After deleting, the next ones show up here.'),
        'kept_heading': gettext('Stay'),
        'kept_note': gettext(
            'Younger than 24 hours. One of them may be in an open tournament'
            ' wizard right now.'
        ),
        'label_none': gettext('Nothing selected'),
    }

    if action_id == 'unused-images':
        return {
            **texts,
            'lead': gettext(
                'These images were uploaded for "%(party)s", and no'
                ' tournament uses them. Untick everything that should stay.',
                party=party_title,
            ),
            'delete_hint': gettext('Deleted images cannot be restored.'),
            'delete_button': gettext('Delete selected images'),
            'label_one': gettext('Delete %(count)s image (%(size)s)'),
            'label_many': gettext('Delete %(count)s images (%(size)s)'),
        }

    if action_id == 'orphaned-files':
        return {
            **texts,
            'lead': gettext(
                'These files are in the image folder of "%(party)s", but none'
                ' of them has an entry yet. No tournament and no image'
                ' picker shows them.',
                party=party_title,
            ),
            'delete_hint': gettext('Deleted files cannot be restored.'),
            'delete_button': gettext('Delete selected files'),
            'label_one': gettext('Delete %(count)s file (%(size)s)'),
            'label_many': gettext('Delete %(count)s files (%(size)s)'),
        }

    abort(404)


def _maintenance_texts(action_id: str) -> dict[str, str]:
    """Return the static texts of the maintenance action."""
    if action_id == 'unused-images':
        return {
            'title': gettext('Unused tournament images'),
            'description': gettext(
                'Uploaded images that no tournament uses, for example from'
                ' abandoned tournament wizards.'
            ),
            'empty': gettext('Nothing to clean up.'),
            'empty_hint': gettext(
                'An unused image appears here 24 hours after its upload.'
            ),
        }

    if action_id == 'orphaned-files':
        return {
            'title': gettext('Orphaned image files'),
            'description': gettext(
                "Files in the party's image folder without an entry. They no"
                ' longer show up anywhere.'
            ),
            'empty': gettext('Nothing to clean up.'),
            'empty_hint': '',
        }

    abort(404)


def _maintenance_finding(
    action_id: str,
    summary: tournament_maintenance_service.MaintenanceSummary,
    size: str,
) -> tuple[Markup, str | None]:
    """Return the finding sentence and the sentence about kept items."""
    kept_finding = (
        ngettext(
            '%(count)s more is younger than 24 hours and stays.',
            '%(count)s more are younger than 24 hours and stay.',
            summary.kept_count,
            count=summary.kept_count,
        )
        if summary.kept_count
        else None
    )

    if action_id == 'unused-images':
        finding = ngettext(
            '<strong>%(count)s image</strong>, %(size)s.',
            '<strong>%(count)s images</strong>, together %(size)s.',
            summary.count,
            count=summary.count,
            size=size,
        )
    elif action_id == 'orphaned-files':
        finding = ngettext(
            '<strong>%(count)s file</strong>, %(size)s.',
            '<strong>%(count)s files</strong>, together %(size)s.',
            summary.count,
            count=summary.count,
            size=size,
        )
    else:
        abort(404)

    # Safe: `count` is an int and `size` comes from `format_file_size`.
    return Markup(finding), kept_finding  # noqa: S704


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


def _admin_match_projections(tournament: Tournament) -> tuple[list[dict], dict]:
    """Batch the backend's tournament scope without initializing any facts.

    Public display fields are allowlisted; history stays in privileged detail.
    Contestant row order remains unchanged for scores and bracket feeders.
    """
    matches = tournament_match_service.get_matches_for_tournament_ordered(
        tournament.id
    )
    contestants_by_match_id = (
        tournament_match_service.get_contestants_for_tournament(tournament.id)
    )
    readiness_by_match_id = build_match_readiness_projections(
        tournament, matches, contestants_by_match_id
    )
    return [
        {
            'match': match,
            'contestants': contestants_by_match_id.get(match.id, []),
            'readiness': readiness_by_match_id[match.id],
            'readiness_display': serialize_public_match_readiness(
                readiness_by_match_id[match.id]
            ),
        }
        for match in matches
    ], readiness_by_match_id


@blueprint.get('/tournaments/<tournament_id>/matches')
@permission_required('lan_tournament.view')
@templated
def matches_for_tournament(tournament_id):
    """List matches for that tournament."""
    tournament = _get_tournament_or_404(tournament_id)
    party = party_service.get_party(tournament.party_id)

    match_data, readiness_by_match_id = _admin_match_projections(tournament)
    matches = [entry['match'] for entry in match_data]
    match_quantities = count_match_projections(
        list(readiness_by_match_id.values())
    )
    filter_options = match_filter_options(match_quantities)
    only = active_match_filter(request.args.get('only', 'all'), filter_options)
    selected_ids = {
        projection.match_id for projection in filter_match_projections(
            list(readiness_by_match_id.values()), only=only
        )
    }
    match_data = [
        entry for entry in match_data if entry['match'].id in selected_ids
    ]
    readiness_by_match_id = {
        entry['match'].id: entry['readiness'] for entry in match_data
    }
    all_contestants = [entry['contestants'] for entry in match_data]

    teams_by_id, participants_by_id = build_contestant_name_lookups(
        tournament.id, all_contestants
    )

    seats_by_user_id, team_members_by_team_id = build_hover_lookups(
        tournament, participants_by_id, teams_by_id, party.id
    )

    match_labels = (
        phase_match_labels(tournament, matches)
        if tournament.has_playoffs
        else {}
    )

    return {
        'party': party,
        'tournament': tournament,
        'match_data': match_data,
        'match_labels': match_labels,
        'only': only,
        'match_quantities': match_quantities,
        'match_filter_options': filter_options,
        'readiness_by_match_id': readiness_by_match_id,
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
    is_ffa = match_uses_placements(tournament, match)
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

    readiness = build_match_readiness_projections(
        tournament, [match], {match.id: contestants}
    )[match.id]
    # Resolve the sides from the pairing, never from association-row order.
    readiness_contestants_by_side = {}
    if readiness.pairing_valid:
        pairing = tournament_repository.get_match_pairing(match.id)
        if pairing is not None:
            for contestant in real_contestants(contestants):
                side = side_for_contestant(
                    contestants, contestant.id, pairing=pairing
                )
                if side is not None:
                    readiness_contestants_by_side[side] = contestant

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
        'is_ffa': is_ffa,
        'ffa_result_consumed': ffa_result_consumed,
        'correction_case': correction_case,
        'ack_match_ids': ack_match_ids,
        'correction_clears_winner': correction_clears_winner,
        'downstream_impact': downstream_impact,
        'max_match_score': tournament_match_service.MAX_MATCH_SCORE,
        'can_unrelease': _playoff_release_can_be_undone(tournament, match),
        'affected_downstream_matches': affected_downstream_matches,
        'readiness': readiness,
        'readiness_contestants_by_side': readiness_contestants_by_side,
        'dashboard_back': _dashboard_back_link(tournament, match),
    }


def _playoff_release_can_be_undone(tournament, match):
    """Tell whether the lock on a group result can still be lifted."""
    if not (
        tournament.has_playoffs is True
        and tournament.playoff_released_at is not None
        and match.phase == 1
    ):
        return True
    progress = tournament_qualification_service.get_phase_two_progress(
        tournament.id
    )
    return not progress.has_result


@blueprint.post('/matches/<match_id>/correct_result')
@permission_required('lan_tournament.administrate')
def correct_match_result(match_id):
    """Correct a match result: retract it and optionally re-enter scores."""
    match = _get_match_or_404(match_id)
    tournament = _get_tournament_or_404(match.tournament_id)

    # FFA matches have no bracket cascade to correct.
    if match_uses_placements(tournament, match):
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
    if not match_uses_placements(tournament, match):
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
                    error=gettext(error_message),
                )
            )

    return redirect_to('.view_match', match_id=match_id)


def _ffa_cut_tie_context(tournament):
    """Return the cut ties and script texts for the FFA pages, orgas only."""
    may_decide = g.user.has_permission('lan_tournament.administrate')
    return {
        'ffa_cut_ties': (
            ffa_cut_ties_payload(tournament.id) if may_decide else []
        ),
        'js_strings': qualification_js_strings(),
    }


@blueprint.get('/tournaments/<tournament_id>/bracket')
@permission_required('lan_tournament.view')
@templated
def bracket(tournament_id):
    """Show tournament bracket visualization."""
    tournament = _get_tournament_or_404(tournament_id)
    party = party_service.get_party(tournament.party_id)

    match_data, readiness_by_match_id = _admin_match_projections(tournament)
    all_contestants = [entry['contestants'] for entry in match_data]
    match_labels = (
        phase_match_labels(tournament, [entry['match'] for entry in match_data])
        if tournament.has_playoffs else {}
    )

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

    # With a playoff phase the page shows phase 1 as ranking cards and
    # draws phase 2 in its own format.
    phases = None
    bracket_match_data = match_data
    if tournament.has_playoffs:
        bracket_match_data = [e for e in match_data if e['match'].phase == 2]
        match tournament_qualification_service.get_qualification(tournament.id):
            case Ok(state) if state.source in (
                tournament_qualification_service.SOURCE_GROUPS,
                tournament_qualification_service.SOURCE_LEADERBOARD,
            ):
                phases = {
                    'source': state.source,
                    'rankings': participant_rankings(
                        state,
                        contestant_names(tournament.id),
                        qualification_strings(),
                        tournament,
                    ),
                    'waiting': playoff_waiting_reason(state, tournament),
                }
            case _:
                pass

    # Round-robin: compute standings table.
    standings = None
    if tournament_match_service.is_plain_round_robin(tournament):
        standings = build_round_robin_standings(match_data)

    ffa_mode = ffa_elimination_mode(tournament)
    ffa_view = None
    if ffa_mode is not None and not (phases and phases['waiting']):
        ffa_view = (
            'de' if ffa_mode == EliminationMode.DOUBLE_ELIMINATION else 'single'
        )

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
    if ffa_view is not None:
        (
            ffa_standings,
            ffa_round_standings,
            _ffa_match_data,
            _ffa_all_contestants,
            ffa_latest_round,
            ffa_all_confirmed,
        ) = _build_ffa_round_data(tournament.id, match_data=match_data)

        # DE: compute per-pool standings and pool status.
        if ffa_view == 'de':
            pool_data = _build_ffa_de_pool_data(tournament.id, _ffa_match_data)
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
        'bracket_match_data': bracket_match_data,
        'readiness_by_match_id': readiness_by_match_id,
        'match_labels': match_labels,
        'bracket_mode': (
            tournament_domain_service.elimination_mode_for_phase(tournament, 2)
            if tournament.has_playoffs
            else tournament.elimination_mode
        ),
        'phases': phases,
        'ffa_view': ffa_view,
        'ffa_gf_offer': (
            ffa_grand_final_offer(tournament) if ffa_view == 'de' else None
        ),
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
        **_ffa_cut_tie_context(tournament),
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


def _build_ffa_round_data(tournament_id: TournamentID, *, match_data=None):
    """Build per-round and cumulative FFA standings from match data.

    Returns (cumulative_standings, round_standings, match_data,
    all_contestants, latest_round, all_confirmed).
    Bracket GET supplies its existing batch; other callers retain their reader.
    """
    if match_data is None:
        match_data = _build_ffa_match_data_list(tournament_id)

    # Group matches by round.
    rounds_map: dict[int, list] = {}
    all_contestants: list[list] = []
    for entry in match_data:
        m, contestants = entry['match'], entry['contestants']
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
        **_ffa_cut_tie_context(tournament),
    }


@blueprint.post('/tournaments/<tournament_id>/generate_ffa_round')
@permission_required('lan_tournament.administrate')
def generate_ffa_round_action(tournament_id):
    """Send the orga to the seeding; generate nothing."""
    tournament = _get_tournament_or_404(tournament_id)

    if tournament.game_format != GameFormat.FREE_FOR_ALL:
        flash_error(gettext('This tournament is not a Free-for-All format.'))
        return redirect_to('.view', tournament_id=tournament.id)

    # Rounds come from the seeding, never from this route.
    return redirect_to('.seeding', tournament_id=tournament.id)


@blueprint.post('/tournaments/<tournament_id>/advance_ffa_round')
@permission_required('lan_tournament.administrate')
def advance_ffa_round_action(tournament_id):
    """Draft the next FFA round and send the orga to its seeding.

    For DE tournaments, the ``pool`` POST parameter selects which pool
    to advance (``WB`` or ``LB``). The lobbies are generated from the
    draft, never here.
    """
    tournament = _get_tournament_or_404(tournament_id)

    elimination_mode = ffa_elimination_mode(tournament)
    if elimination_mode is None:
        flash_error(gettext('This tournament is not a Free-for-All format.'))
        return redirect_to('.view', tournament_id=tournament.id)

    # DE: read pool parameter from form POST data.
    pool = None
    if elimination_mode == EliminationMode.DOUBLE_ELIMINATION:
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

    match tournament_seeding_service.prepare_ffa_round_draft(
        tournament.id, pool=pool, initiator_id=g.user.id
    ):
        case Ok('completed'):
            flash_success(gettext('The tournament is complete. The lone survivor wins.'))
            return redirect_to('.bracket', tournament_id=tournament.id)
        case Ok(target):
            return redirect_to(
                '.seeding', tournament_id=tournament.id, target=target
            )
        case Err(error_message):
            flash_error(gettext(error_message))

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
                    error=gettext(error_message),
                )
            )

    return redirect_to('.view_match', match_id=match_id)


@blueprint.post('/tournaments/<tournament_id>/generate_ffa_grand_final')
@permission_required('lan_tournament.administrate')
def generate_ffa_grand_final_action(tournament_id):
    """Generate the Grand Final round for an FFA-DE tournament."""
    tournament = _get_tournament_or_404(tournament_id)

    refusal = ffa_grand_final_refusal(tournament)
    if refusal is not None:
        flash_error(gettext(refusal))
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
                    error=gettext(error_message),
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


# -------------------------------------------------------------------- #
# orga dashboard

_DASHBOARD_PERMISSION = 'lan_tournament.administrate'
_DASHBOARD_PAGE_TEMPLATE = 'admin/lan_tournament/dashboard.html'
_DASHBOARD_PANEL_TEMPLATE = 'common/lan_tournament/_dashboard_panel.html'

# The transport code (`invalid`, 422) of a form the server refuses, whatever
# field is at fault. The message is the form's own.
_DASHBOARD_FORM_INVALID_ERROR = DASHBOARD_QUERY_INVALID_ERROR

# Refusals that say nothing about the list: no rows and no fragment.
_DASHBOARD_BARE_CODES = frozenset(
    {
        TRANSPORT_ERROR_SESSION_EXPIRED,
        TRANSPORT_ERROR_ACCESS_REVOKED,
        TRANSPORT_ERROR_UNAVAILABLE,
    }
)


def _dashboard_access(*, json_only: bool = False):
    """Gate a dashboard route and refuse so that its client can read it.

    The core decorators answer an anonymous request with a redirect to the
    login form and a missing permission with an HTML page. A poll or a JSON
    request needs the status and the stable `error` code instead.
    """

    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            json_wanted = json_only or wants_json(request)

            if not g.user.authenticated:
                if json_wanted:
                    return _dashboard_json_error(
                        DASHBOARD_UNAUTHENTICATED_ERROR
                    )
                return _dashboard_login_redirect(kwargs['party_id'])

            if not g.user.has_permission(_DASHBOARD_PERMISSION):
                if json_wanted:
                    return _dashboard_json_error(DASHBOARD_FORBIDDEN_ERROR)
                abort(403)

            return func(*args, **kwargs)

        return wrapper

    return decorator


def _dashboard_login_redirect(party_id):
    flash_notice(gettext('Please log in.'))
    return _dashboard_no_store(
        redirect_to(
            'authn_login_admin.log_in_form',
            next=url_for(
                'lan_tournament_admin.dashboard_for_party', party_id=party_id
            ),
        )
    )


def _dashboard_no_store(response):
    response.headers['Cache-Control'] = 'private, no-store'
    return response


def _dashboard_json(body, status: int = 200):
    return _dashboard_no_store(make_response(jsonify(body), status))


def _dashboard_json_error(
    error: str,
    *,
    message: str | None = None,
    fragment=None,
    draft_target: bool | None = None,
    detail: str | None = None,
):
    body, status = serialize_dashboard_error(
        error, fragment=fragment, draft_target=draft_target
    )
    if message is not None:
        body['message'] = message
    if detail is not None:
        body['detail'] = detail

    return _dashboard_json(body, status)


def _dashboard_page(
    party, context, *, status=200, action=None, unavailable=None
):
    html = render_template(
        _DASHBOARD_PAGE_TEMPLATE,
        dashboard=context,
        party=party,
        action=action,
        unavailable=unavailable,
    )
    return _dashboard_no_store(make_response(html, status))


def _dashboard_fragment(context, page, settings: DashboardSettings):
    html = render_template(
        _DASHBOARD_PANEL_TEMPLATE, dashboard=context, action=None
    )
    return serialize_dashboard_fragment(
        html, as_of=page.as_of, poll_seconds=settings.poll_seconds
    )


def _dashboard_settings(party_id) -> DashboardSettings:
    """Return the effective settings. A broken configuration raises."""
    return get_effective_dashboard_settings(party_id).unwrap()


def _read_dashboard(party_id, settings, query, query_errors):
    """Return the page and its render context from one fresh snapshot."""
    page_result = get_dashboard_page(g.user, party_id, query, settings=settings)
    if page_result.is_err():
        return Err(page_result.unwrap_err())
    page = page_result.unwrap()

    context = build_dashboard_context(
        page,
        query,
        settings,
        surface='admin',
        party_id=party_id,
        csrf_token=get_dashboard_csrf_token(g.user),
        query_errors=query_errors,
    )
    return Ok((page, context))


def _read_requested_dashboard(party_id, settings):
    """Read the list that the query string of the request asks for.

    The scope is resolved from the validated query and read again by the
    page itself, which stays the authority. It only decides whether a
    tournament filter lies inside it.
    """
    first, _ = parse_dashboard_query(
        request.args, surface='admin', per_page=settings.page_size
    )
    scope_result = resolve_dashboard_scope(g.user, party_id, first.scope)
    if scope_result.is_err():
        return Err(scope_result.unwrap_err())

    query, errors = parse_dashboard_query(
        request.args,
        surface='admin',
        per_page=settings.page_size,
        scope_tournament_ids=scope_result.unwrap().tournament_ids,
    )
    return _read_dashboard(party_id, settings, query, errors)


@blueprint.get('/for_party/<party_id>/dashboard')
@login_required
@permission_required('lan_tournament.administrate')
def dashboard_for_party(party_id):
    """Show the orga dashboard of a party across its tournaments."""
    party = _get_party_or_404(party_id)
    settings = _dashboard_settings(party.id)

    read = _read_requested_dashboard(party.id, settings)
    if read.is_err():
        abort(TRANSPORT_ERRORS[read.unwrap_err()][1])
    _, context = read.unwrap()

    return _dashboard_page(party, context)


@blueprint.get('/for_party/<party_id>/dashboard/poll')
@_dashboard_access(json_only=True)
def dashboard_poll_for_party(party_id):
    """Answer the rendered dashboard panel as JSON, for the refresh."""
    settings = _dashboard_settings(party_id)

    read = _read_requested_dashboard(party_id, settings)
    if read.is_err():
        return _dashboard_json_error(read.unwrap_err())
    page, context = read.unwrap()

    return _dashboard_json(_dashboard_fragment(context, page, settings))


@blueprint.post('/for_party/<party_id>/dashboard/matches/<match_id>/pin')
@_dashboard_access()
def dashboard_pin(party_id, match_id):
    """Pin a match for every orga, or take the pin away."""
    return _dashboard_action(party_id, match_id, 'pin')


@blueprint.post('/for_party/<party_id>/dashboard/matches/<match_id>/ack')
@_dashboard_access()
def dashboard_ack(party_id, match_id):
    """Record that an orga checked the delay of a due match."""
    return _dashboard_action(party_id, match_id, 'ack')


def _dashboard_action(party_id, match_id, kind: str):
    """Check the request, run the pin or acknowledgement, answer it.

    The party comes from the URL and is bound to the match by the service.
    Every hidden field is untrusted; the service checks authority and the
    expected revision again under its locks.
    """
    if kind == 'ack':
        form = DashboardAcknowledgementForm(request.form)
    else:
        form = DashboardPinForm(request.form)

    if validate_dashboard_csrf(g.user, form.csrf_token.data).is_err():
        return _dashboard_refused(
            party_id, match_id, kind, form, CSRF_INVALID_ERROR
        )

    if not form.validate():
        return _dashboard_refused(
            party_id, match_id, kind, form, _DASHBOARD_FORM_INVALID_ERROR
        )

    if kind == 'ack':
        result = acknowledge_match(
            g.user,
            party_id,
            match_id,
            expected_episode_id=form.episode.data,
            expected_ack_revision=form.revision.data,
            comment=form.comment.data,
        )
    else:
        result = set_match_pin(
            g.user,
            party_id,
            match_id,
            pinned=form.pinned.data,
            expected_revision=form.revision.data,
        )

    if result.is_err():
        return _dashboard_refused(
            party_id, match_id, kind, form, result.unwrap_err()
        )

    return _dashboard_accepted(party_id, match_id, kind, form, result.unwrap())


def _dashboard_accepted(party_id, match_id, kind, form, outcome):
    settings = _dashboard_settings(party_id)
    query = _dashboard_return_query(form, settings)

    if not wants_json(request):
        flash_success(_dashboard_success_text(kind, outcome))
        url = build_dashboard_list_url(
            'admin', party_id, query, anchor_match_id=match_id
        )
        return _dashboard_no_store(redirect(url, code=303))

    read = _read_dashboard(party_id, settings, query, {})
    if read.is_err():
        return _dashboard_json_error(read.unwrap_err())
    page, context = read.unwrap()

    if kind == 'ack':
        committed_at = outcome.occurred_at
    else:
        committed_at = outcome.updated_at if outcome is not None else page.as_of

    return _dashboard_json(
        serialize_dashboard_success(
            committed_at=committed_at,
            fragment=_dashboard_fragment(context, page, settings),
        )
    )


def _dashboard_success_text(kind: str, outcome) -> str:
    labels = dashboard_labels()

    if kind == 'ack':
        detail = labels['ack_banner_detail_template'] % {
            'time': format_freshness_time(outcome.occurred_at)
        }
        return f'{labels["ack_announce"]} {detail}'

    if outcome is None or outcome.pinned_at is None:
        return labels['pin_removed']

    return labels['pin_ok_template'] % {
        'actor': g.user.screen_name,
        'time': format_wall_time(
            outcome.pinned_at, snapshot=outcome.updated_at
        ),
        'server': format_freshness_time(outcome.updated_at),
    }


def _dashboard_refused(party_id, match_id, kind, form, error):
    """Answer a refused pin or acknowledgement.

    A stale, refused or invalid request carries the list as it is now, so
    the client sees the server state next to its draft. A refusal about
    access or about the match says nothing of the list.
    """
    code = TRANSPORT_ERRORS[error][0]
    json_wanted = wants_json(request)

    if code in _DASHBOARD_BARE_CODES or (
        json_wanted and code == TRANSPORT_ERROR_CSRF_INVALID
    ):
        return _dashboard_bare_refusal(party_id, form, error)

    settings = _dashboard_settings(party_id)
    query = _dashboard_return_query(form, settings)
    read = _read_dashboard(party_id, settings, query, {})
    if read.is_err():
        return _dashboard_bare_refusal(party_id, form, read.unwrap_err())
    page, context = read.unwrap()

    key = _dashboard_match_key(match_id)
    row = next((r for r in context['rows'] if r['match_id'] == key), None)
    draft_target = row is not None and bool(row[kind]['offered'])
    field, field_text = None, None
    if error == _DASHBOARD_FORM_INVALID_ERROR:
        field, field_text = _dashboard_form_error(form)

    if json_wanted:
        return _dashboard_json_error(
            error,
            message=field_text,
            fragment=_dashboard_fragment(context, page, settings),
            draft_target=draft_target,
            detail=_dashboard_json_action_detail(error, kind, code, row),
        )

    labels = dashboard_labels()
    draft = form.comment.data if kind == 'ack' else None
    action = {
        'kind': kind,
        'match_id': key if row is not None else None,
        'error': code,
        'message': _dashboard_action_message(
            error, kind, code, labels, field_text
        ),
        'detail': _dashboard_action_detail(error, kind, code, labels, row),
        'draft': draft,
        'draft_target': draft_target,
        'field_error': (
            {'field': field, 'text': field_text} if field is not None else None
        ),
    }
    if draft_target and kind == 'ack':
        text = draft or ''
        row['ack']['form'].update(
            {
                'open': True,
                'draft': text,
                'counter_text': format_comment_counter(len(text)),
                'is_over': len(text) > MAX_COMMENT_LENGTH,
                'error_text': field_text if field == 'comment' else None,
            }
        )

    return _dashboard_page(
        _get_party_or_404(party_id),
        context,
        status=TRANSPORT_ERRORS[error][1],
        action=action,
    )


def _dashboard_bare_refusal(party_id, form, error):
    if wants_json(request):
        return _dashboard_json_error(error)

    code, status = TRANSPORT_ERRORS[error]
    if code == TRANSPORT_ERROR_SESSION_EXPIRED:
        return _dashboard_login_redirect(party_id)
    if code == TRANSPORT_ERROR_UNAVAILABLE:
        return _dashboard_unavailable_page(party_id, form)

    abort(status)


def _dashboard_unavailable_page(party_id, form):
    """Answer a missing and a hidden match with one and the same page."""
    settings = _dashboard_settings(party_id)
    query = _dashboard_return_query(form, settings)
    party = _get_party_or_404(party_id)
    labels = dashboard_labels()

    return _dashboard_page(
        party,
        None,
        status=404,
        unavailable={
            'heading': labels['missing_heading'],
            'detail': labels['missing_detail'],
            'back_label': labels['link_back_plain'],
            'back_url': build_dashboard_list_url('admin', party.id, query),
        },
    )


def _dashboard_return_query(
    form, settings: DashboardSettings
) -> DashboardQuery:
    """Return the list the form came from, the default one if unusable."""
    query = parse_dashboard_return(
        form.return_to.data, surface='admin', per_page=settings.page_size
    )
    if query is None:
        return DashboardQuery(per_page=settings.page_size)

    return query


def _dashboard_back_link(tournament, match=None) -> dict[str, str] | None:
    """Return the link back to the list a page was opened from, if any.

    Only a viewer who may open the dashboard gets it. The `return` value is
    read, never followed: the URL is rebuilt from the tournament's party and
    the validated query, and a broken configuration leaves the page as it was.
    """
    raw = request.args.get('return')
    if not raw or not g.user.has_permission(_DASHBOARD_PERMISSION):
        return None

    settings_result = get_dashboard_settings()
    if settings_result.is_err():
        return None

    query = parse_dashboard_return(
        raw,
        surface='admin',
        per_page=settings_result.unwrap().page_size,
    )
    if query is None:
        return None

    return {
        'url': build_dashboard_list_url(
            'admin',
            tournament.party_id,
            query,
            anchor_match_id=match.id if match is not None else None,
        ),
        'label': dashboard_labels()['link_back'],
        'context': describe_dashboard_return(query),
    }


def _dashboard_match_key(raw) -> str | None:
    """Return the canonical text of a match ID from a URL, if it is one."""
    try:
        return str(UUID(str(raw)))
    except ValueError:
        return None


def _dashboard_form_error(form) -> tuple[str, str]:
    """Return the field and text of the error to show, the comment first."""
    errors = form.errors
    field = 'comment' if 'comment' in errors else next(iter(errors))

    return field, str(errors[field][0])


def _dashboard_action_message(error, kind, code, labels, field_text) -> str:
    if error == CSRF_INVALID_ERROR:
        return str(CSRF_INVALID_NOTICE)

    if error == _DASHBOARD_FORM_INVALID_ERROR:
        return field_text

    reason = gettext(error)
    if kind == 'ack' and code == TRANSPORT_ERROR_REFUSED:
        return labels['ack_refused_template'] % {'reason': reason}

    return reason


def _dashboard_json_action_detail(error, kind, code, row) -> str | None:
    """Return the sentence a script shows under a stale check, if any.

    It is the sentence of the browser page, from the same re-read row, so
    both name the other orga's record or neither does.
    """
    if code != TRANSPORT_ERROR_STALE:
        return None

    return _dashboard_action_detail(error, kind, code, dashboard_labels(), row)


def _dashboard_action_detail(error, kind, code, labels, row) -> str | None:
    if kind != 'ack':
        return None

    if error == DASHBOARD_ACK_CONFLICT_ERROR:
        record = row['ack']['record'] if row is not None else None
        if record is None:
            return None
        return labels['ack_stale_detail_template'] % {
            'actor': record['actor'],
            'time': record['time'],
        }

    if code == TRANSPORT_ERROR_REFUSED:
        return labels['ack_refused_detail']

    return None
