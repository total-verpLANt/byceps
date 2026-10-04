from datetime import datetime, UTC
from unittest.mock import patch

import pytest
from sqlalchemy import event
from sqlalchemy.engine import Engine

from byceps.database import db
from byceps.services.lan_tournament import (
    lan_tournament_view_helpers as helpers,
    tournament_log_service,
    tournament_match_service,
    tournament_orga_service,
    tournament_qualification_service,
    tournament_repository,
    tournament_seeding_service,
    tournament_service,
)
from byceps.services.lan_tournament.blueprints.admin import views as admin_views
from byceps.services.lan_tournament.blueprints.site import views as site_views
from byceps.services.lan_tournament.models import ContestantType
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.playoff import PlayoffReleaseMode
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipant,
    TournamentParticipantID,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.util.uuid import generate_uuid7

from tests.helpers import log_in_user


@pytest.fixture
def page_setup(party, make_user, make_admin):
    admin = make_admin(
        {'admin.access', 'lan_tournament.administrate', 'lan_tournament.view'}
    )
    log_in_user(admin.id)
    tournament = tournament_service.create_tournament(
        party.id,
        f'Query budget {generate_uuid7()}',
        contestant_type=ContestantType.SOLO,
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.ROUND_ROBIN,
        tournament_status=TournamentStatus.REGISTRATION_CLOSED,
        playoff_game_format=GameFormat.ONE_V_ONE,
        playoff_elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        playoff_group_count=4,
        playoff_qualifiers_per_group=2,
        playoff_release_mode=PlayoffReleaseMode.MANUAL,
    ).unwrap()[0]
    for i in range(16):
        user = make_user(f'Budget{i}-{generate_uuid7()}')
        tournament_repository.create_participant(
            TournamentParticipant(
                id=TournamentParticipantID(generate_uuid7()),
                user_id=user.id,
                tournament_id=tournament.id,
                substitute_player=False,
                team_id=None,
                created_at=datetime.now(UTC),
            )
        )
    db.session.commit()
    assert tournament_match_service.generate_round_robin_bracket(
        tournament.id
    ).is_ok()
    assert tournament_service.change_status(
        tournament.id, TournamentStatus.ONGOING, admin.id
    ).is_ok()
    for match in tournament_repository.get_matches_for_tournament(
        tournament.id
    ):
        contestants = tournament_repository.get_contestants_for_match(match.id)
        ids = sorted([c.participant_id for c in contestants], key=str)
        assert tournament_match_service.admin_set_and_confirm_match(
            match.id, admin.id, {ids[0]: match.group_order + 1, ids[1]: 0}
        ).is_ok()
    state = tournament_qualification_service.get_qualification(
        tournament.id
    ).unwrap()
    for blocker in state.blockers:
        assert tournament_qualification_service.save_decision(
            tournament.id,
            blocker.scope,
            list(blocker.contestant_ids),
            reason='Budget fixture tiebreak',
            initiator_id=admin.id,
        ).is_ok()
    assert (
        tournament_qualification_service.get_qualification(tournament.id)
        .unwrap()
        .ready
    )
    assert tournament_seeding_service.ensure_playoff_draft(
        tournament.id
    ).is_ok()
    assert tournament_orga_service.assign_orga(
        tournament.id, admin.id, admin.id
    ).is_ok()
    yield tournament, admin
    tournament_service.delete_tournament(tournament.id)


@pytest.mark.parametrize(
    'is_site,budget', [(False, 32), (True, 35)], ids=['admin', 'site']
)
def test_qualification_page_stays_within_its_query_budget(
    page_setup, make_client, admin_app, site_app, is_site, budget
):
    tournament, admin = page_setup
    views = site_views if is_site else admin_views
    client = make_client(site_app if is_site else admin_app, user_id=admin.id)
    statements = []
    snapshots = []
    names_maps = []
    original_qualification = tournament_qualification_service.get_qualification

    def compute_qualification(*args, **kwargs):
        result = original_qualification(*args, **kwargs)
        snapshots.append(result.unwrap())
        return result

    def compute_names(*args, **kwargs):
        result = helpers.contestant_names(*args, **kwargs)
        names_maps.append(result)
        return result

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(Engine, 'before_cursor_execute', record)
    try:
        payload_name = (
            '_orga_qualification_payload'
            if is_site
            else '_qualification_payload'
        )
        with (
            patch.object(
                tournament_qualification_service,
                'get_qualification',
                side_effect=compute_qualification,
            ) as qualification,
            patch.object(
                tournament_seeding_service,
                'get_board',
                wraps=tournament_seeding_service.get_board,
            ) as board,
            patch.object(
                views, 'contestant_names', side_effect=compute_names
            ) as names,
            patch.object(
                views, payload_name, wraps=getattr(views, payload_name)
            ) as payload,
            patch.object(
                views,
                'seeding_audit_context',
                wraps=helpers.seeding_audit_context,
            ) as audit_context,
        ):
            host = 'www' if is_site else 'admin'
            orga = '/orga' if is_site else ''
            response = client.get(
                f'http://{host}.acmecon.test/lan-tournaments{orga}/tournaments/{tournament.id}/qualification'
            )
            assert response.status_code == 200
            print(
                f'{host.upper()} qualification page: {len(statements)} queries (budget {budget})'
            )
            assert len(statements) <= budget
            assert (
                qualification.call_count
                == board.call_count
                == names.call_count
                == 1
            )
            assert board.call_args.kwargs['qualification'] is snapshots[0]
            assert payload.call_args.args[1] is snapshots[0]
            assert (
                payload.call_args.args[2]
                is audit_context.call_args.args[1]
                is names_maps[0]
            )
            audit = [
                sql
                for sql in statements
                if 'FROM lan_tournament_log_entries' in sql
            ]
            assert len(audit) == 1
            assert 'LIMIT' in audit[0]
    finally:
        event.remove(Engine, 'before_cursor_execute', record)


@pytest.mark.parametrize(
    'is_site,page',
    [
        (False, 'qualification'),
        (True, 'qualification'),
        (False, 'seeding'),
        (True, 'seeding'),
    ],
)
def test_audit_query_is_bounded(
    page_setup, make_client, admin_app, site_app, is_site, page
):
    tournament, admin = page_setup
    for _ in range(201):
        tournament_log_service.create_log_entry(
            'participant-added', tournament.id, None, commit=False
        )
    db.session.commit()
    client = make_client(site_app if is_site else admin_app, user_id=admin.id)
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        if 'FROM lan_tournament_log_entries' in statement:
            statements.append(statement)

    event.listen(Engine, 'before_cursor_execute', record)
    try:
        host = 'www' if is_site else 'admin'
        orga = '/orga' if is_site else ''
        response = client.get(
            f'http://{host}.acmecon.test/lan-tournaments{orga}/tournaments/{tournament.id}/{page}?target=playoff'
        )
        assert response.status_code == 200
        assert len(statements) == 1
        assert 'LIMIT' in statements[0]
        assert (
            'Only the latest 200 entries are shown.'
            in response.get_data(as_text=True)
            or 'Nur die letzten 200 Einträge werden angezeigt.'
            in response.get_data(as_text=True)
        )
    finally:
        event.remove(Engine, 'before_cursor_execute', record)
