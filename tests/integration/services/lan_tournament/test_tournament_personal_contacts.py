from dataclasses import replace
from datetime import datetime, UTC
from types import SimpleNamespace

from babel import Locale
from flask import g, url_for
from jinja2 import ChoiceLoader, FileSystemLoader
import pytest
from sqlalchemy import update

from byceps.database import db
from byceps.services.lan_tournament import (
    tournament_personal_repository as repository,
)
from byceps.services.lan_tournament.dbmodels.team import DbTournamentTeam
from byceps.services.lan_tournament.lan_tournament_view_helpers import (
    build_match_label,
)
from byceps.services.lan_tournament.models.contestant_type import ContestantType
from byceps.services.lan_tournament.models.tournament_team import TournamentTeam
from byceps.services.seating.dbmodels.area import DbSeatingArea
from byceps.services.seating.dbmodels.seat import DbSeat
from byceps.services.ticketing.dbmodels.ticket import DbTicket
from byceps.util.uuid import generate_uuid7

from tests.helpers import http_client, log_in_user

from . import test_tournament_personal_overview as source
from .test_tournament_personal_overview import queries


pytestmark = pytest.mark.usefixtures('admin_app')
setup = source.setup
site_app_for_setup = source.site_app_for_setup


@pytest.fixture(params=['standard', 'gv36'])
def contact_app(site_app_for_setup, request):
    app = site_app_for_setup
    if request.param == 'gv36':
        app.jinja_env.loader = ChoiceLoader(
            [
                FileSystemLoader('sites/totalverplant-36/template_overrides'),
                app.jinja_env.loader,
            ]
        )
    return app


def make_seat(category, label, *, area=None):
    if area is None:
        slug = str(generate_uuid7())
        area = DbSeatingArea(
            generate_uuid7(), category.party_id, slug, f'Hall {slug}'
        )
        db.session.add(area)
    seat = DbSeat(generate_uuid7(), area.id, category.id, label=label)
    seat.area = area
    db.session.add(seat)
    return seat


def make_ticket(category, owner, user, seat=None, *, revoked=False):
    ticket = DbTicket(
        generate_uuid7(),
        datetime.now(UTC),
        category,
        str(generate_uuid7()),
        owner.id,
        used_by_id=user.id if user else None,
        revoked=revoked,
    )
    ticket.occupied_seat_id = seat.id if seat else None
    db.session.add(ticket)
    return ticket


def test_seats_use_current_ticket_user_and_party_not_owner_or_manager(
    make_party, brand, make_user, make_ticket_category
):
    party, foreign = make_party(brand), make_party(brand)
    category = make_ticket_category(party.id, 'Seat contacts')
    foreign_category = make_ticket_category(foreign.id, 'Foreign contacts')
    owner, user, manager = make_user(), make_user(), make_user()
    seat_b = make_seat(category, 'B12')
    seat_a = make_seat(category, 'A17', area=seat_b.area)
    ticket = make_ticket(category, owner, user, seat_b)
    ticket.seat_managed_by_id = manager.id
    make_ticket(category, owner, user, seat_a)
    annex = DbSeatingArea(generate_uuid7(), party.id, 'annex', 'Annex')
    db.session.add(annex)
    make_ticket(category, owner, user, make_seat(category, 'C01', area=annex))
    make_ticket(
        category, owner, user, make_seat(category, 'Revoked'), revoked=True
    )
    make_ticket(category, owner, None, make_seat(category, 'Unused'))
    make_ticket(category, owner, user)
    make_ticket(
        foreign_category,
        owner,
        user,
        make_seat(foreign_category, 'Foreign ticket'),
    )
    make_ticket(
        category, owner, user, make_seat(foreign_category, 'Foreign area')
    )
    db.session.commit()

    with queries() as statements:
        seats = repository.get_contact_seats(
            party.id, {owner.id, user.id, manager.id}
        )
        assert [s.label for s in seats[user.id]] == ['C01', 'A17', 'B12']
        assert seats[user.id][0].area.slug
    assert len(statements) == 1
    assert seats[user.id][2].id == seat_b.id
    assert set(seats) == {user.id}

    db.session.execute(
        update(DbTicket).where(DbTicket.id == ticket.id).values(revoked=True)
    )
    db.session.commit()
    assert [
        s.label
        for s in repository.get_contact_seats(party.id, {user.id})[user.id]
    ] == ['C01', 'A17']
    replacement = make_seat(category, 'A18', area=seat_a.area)
    db.session.execute(
        update(DbTicket)
        .where(DbTicket.occupied_seat_id == seat_a.id)
        .values(occupied_seat_id=replacement.id)
    )
    db.session.commit()
    assert [
        s.label
        for s in repository.get_contact_seats(party.id, {user.id})[user.id]
    ] == ['C01', 'A18']


def test_empty_contacts_do_not_query():
    with queries() as statements:
        assert repository.get_contact_seats('unused', set()) == {}
    assert statements == []


def profile_link(app, user):
    with app.test_request_context():
        return url_for('user_profile.view', user_id=user.id)


def test_solo_profiles_multiple_seats_orga_fallbacks_and_render_without_queries(
    setup,
    contact_app,
    make_ticket_category,
    make_admin,
    make_user,
    email_config,
):
    party, user, opponent = setup
    app = contact_app
    orga = make_admin({'lan_tournament.administrate'})
    deleted = make_user(deleted=True)
    t = source.create(
        party, start_time=datetime(2026, 10, 3, 20, 15, tzinfo=UTC)
    )
    for player in [user, opponent]:
        source.add(t, player)
    source.engine.generate_single_elimination_bracket(
        t.id, initiator_id=orga.id
    ).unwrap()
    for responsible in [opponent, orga, deleted]:
        source.tournament_orga_service.assign_orga(
            t.id, responsible.id, orga.id
        ).unwrap()
    category = make_ticket_category(party.id, 'Links')
    seat_a = make_seat(category, 'A17')
    seat_b = make_seat(category, 'B12', area=seat_a.area)
    make_ticket(category, user, opponent, seat_a)
    make_ticket(category, user, opponent, seat_b)
    db.session.commit()
    context = source.personal.get_personal_overview(party.id, user.id)
    assert deleted.id not in context['contact_users_by_id']
    assert orga.id not in context['seats_by_user_id']
    with app.test_request_context('/lan-tournaments/mine'):
        g.user = SimpleNamespace(authenticated=True, locale=Locale('en'))
        with queries() as statements:
            html = app.jinja_env.get_template(
                'site/lan_tournament/_personal_overview.html'
            ).render(
                **context,
                overview_mode='personal',
                has_orga_assignments=True,
                categories=[],
                match_label=build_match_label,
            )
    assert statements == []
    assert f'href="{profile_link(app, opponent)}"' in html
    assert f'href="{profile_link(app, orga)}"' in html
    assert profile_link(app, deleted) not in html
    for seat in [seat_a, seat_b]:
        with app.test_request_context():
            seat_url = url_for(
                'seating.view_area',
                slug=seat.area.slug,
                _anchor=f'seat-{seat.id}',
            )
        # Each appears once for the opponent and once for the orga contact.
        assert html.count(f'href="{seat_url}"') == 2
    fallback = html.split('Ask the tournament orga')[0].rsplit(
        'personal-orga', 1
    )[1]
    assert 'personal-seats' not in fallback
    assert 'Start:' in html
    assert html.count('personal-format-action') == 1
    assert 'Match open' in html

    log_in_user(user.id)
    with http_client(app, user_id=user.id) as client:
        response = client.get('/lan-tournaments/mine')
        assert response.status_code == 200
        assert f'#seat-{seat_b.id}' in response.get_data(as_text=True)
    for responsible in [opponent, orga, deleted]:
        source.tournament_orga_service.revoke_orga(
            t.id, responsible.id, orga.id
        ).unwrap()
    with http_client(app, user_id=user.id) as client:
        html = client.get('/lan-tournaments/mine').get_data(as_text=True)
        assert 'No tournament orga assigned' not in html
        assert 'class="personal-orgas"' not in html
        assert 'class="personal-footer"' in html
        assert 'class="tournament-start"' in html and '2026' in html


def test_team_captain_not_in_participant_lookup_and_captain_changes(
    setup, contact_app, make_user, make_ticket_category, email_config
):
    party, user, opponent = setup
    app = contact_app
    captain = make_user()
    deleted = make_user(deleted=True)
    t = source.create(party, contestant_type=ContestantType.TEAM)
    teams = []
    for player, name, leader in [
        (user, 'Own team', user),
        (opponent, 'Opponent team', captain),
    ]:
        p = source.add(t, player)
        team = TournamentTeam(
            id=generate_uuid7(),
            tournament_id=t.id,
            name=name,
            tag=None,
            description=None,
            image_url=None,
            captain_user_id=leader.id,
            join_code=None,
            created_at=datetime.now(UTC),
        )
        source.repository.create_team(team)
        source.repository.update_participant(replace(p, team_id=team.id))
        teams.append(team)
    source.engine.generate_single_elimination_bracket(
        t.id, initiator_id=user.id
    ).unwrap()
    category = make_ticket_category(party.id, 'Captain seats')
    seat = make_seat(category, 'C42')
    make_ticket(category, user, captain, seat)
    db.session.commit()
    log_in_user(user.id)
    with http_client(app, user_id=user.id) as client:
        html = client.get('/lan-tournaments/mine').get_data(as_text=True)
        assert f'/lan-tournaments/teams/{teams[1].id}' in html
        assert profile_link(app, captain) in html and f'#seat-{seat.id}' in html
        assert 'Captain:' in html and 'Current team' in html
        db.session.execute(
            update(DbTournamentTeam)
            .where(DbTournamentTeam.id == teams[1].id)
            .values(captain_user_id=opponent.id)
        )
        db.session.commit()
        html = client.get('/lan-tournaments/mine').get_data(as_text=True)
        assert (
            profile_link(app, captain) not in html
            and f'#seat-{seat.id}' not in html
        )
        assert (
            profile_link(app, opponent) in html and 'No seat specified' in html
        )
        db.session.execute(
            update(DbTournamentTeam)
            .where(DbTournamentTeam.id == teams[1].id)
            .values(captain_user_id=deleted.id)
        )
        db.session.commit()
        html = client.get('/lan-tournaments/mine').get_data(as_text=True)
        assert f'/lan-tournaments/teams/{teams[1].id}' in html
        assert (
            'No captain available' in html
            and profile_link(app, deleted) not in html
        )


def test_inactive_profiles_are_not_linked_and_orga_seat_is_independent(
    setup, contact_app, make_user, make_ticket_category, email_config
):
    party, user, _ = setup
    app = contact_app
    suspended = make_user(suspended=True)
    uninitialized = make_user(initialized=False)
    t = source.create(party)
    source.add(t, user)
    source.add(t, suspended)
    source.engine.generate_single_elimination_bracket(
        t.id, initiator_id=user.id
    ).unwrap()
    for orga in [user, suspended, uninitialized]:
        source.tournament_orga_service.assign_orga(
            t.id, orga.id, user.id
        ).unwrap()
    category = make_ticket_category(party.id, 'Inactive orga seat')
    seat = make_seat(category, 'D11')
    make_ticket(category, user, suspended, seat)
    db.session.commit()
    log_in_user(user.id)
    with http_client(app, user_id=user.id) as client:
        for path in ['/lan-tournaments/mine', '/lan-tournaments/supervised']:
            html = client.get(path).get_data(as_text=True)
            assert profile_link(app, suspended) not in html
            assert profile_link(app, uninitialized) not in html
            assert suspended.screen_name in html
            assert f'#seat-{seat.id}' in html
