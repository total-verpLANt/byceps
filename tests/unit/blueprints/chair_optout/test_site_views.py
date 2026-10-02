"""
:License: Revised BSD (see `LICENSE` file for details)
"""

from datetime import datetime
from types import SimpleNamespace

from flask import Flask, g, url_for
from flask_babel import Babel, gettext
from jinja2 import Environment
import pytest

from byceps.services.chair_optout.blueprints.site import views
from byceps.services.site.models import SiteID
from byceps.services.ticketing.blueprints.site import views as ticketing_views
from byceps.services.ticketing.dbmodels.ticket import DbTicket
from byceps.services.ticketing.models.ticket import ChairSource
from byceps.util.templating import create_site_template_loader

from tests.helpers import generate_uuid


def _set_request_user_and_party(*, authenticated=True, enabled=True):
    g.user = SimpleNamespace(id=generate_uuid(), authenticated=authenticated)
    g.party = SimpleNamespace(
        id='current-party', ticket_management_enabled=enabled
    )


def test_pending_tickets_are_memoized_only_within_request(app, monkeypatch):
    calls = []
    ticket_ids = [generate_uuid(), generate_uuid()]

    def get_pending(party_id, user_id):
        calls.append((party_id, user_id))
        return ticket_ids

    monkeypatch.setattr(
        views.chair_optout_service,
        'get_pending_chair_ticket_ids_for_user',
        get_pending,
    )
    with app.test_request_context('/'):
        _set_request_user_and_party()
        assert views.get_pending_chair_ticket_ids() == ticket_ids
        assert views.find_first_unanswered_chair_ticket_id() == ticket_ids[0]
        assert len(calls) == 1

    # Test clients can reuse an app context across requests. The cache must
    # still expire at the request boundary.
    with app.app_context():
        for _ in range(2):
            with app.test_request_context('/'):
                _set_request_user_and_party()
                assert views.get_pending_chair_ticket_ids() == ticket_ids
    assert len(calls) == 3


@pytest.mark.parametrize('state', ['anonymous', 'no_party', 'disabled'])
def test_pending_lookup_skips_ineligible_request(app, monkeypatch, state):
    def unexpected_lookup(*_):
        pytest.fail('Ineligible requests must not query pending tickets')

    monkeypatch.setattr(
        views.chair_optout_service,
        'get_pending_chair_ticket_ids_for_user',
        unexpected_lookup,
    )
    with app.test_request_context('/'):
        _set_request_user_and_party(
            authenticated=state != 'anonymous', enabled=state != 'disabled'
        )
        if state == 'no_party':
            g.party = None
        assert views.get_pending_chair_ticket_ids() == []
        assert views.find_first_unanswered_chair_ticket_id() is None


@pytest.mark.parametrize(
    'state',
    [
        'eligible',
        'anonymous',
        'no_party',
        'disabled',
        'foreign_party',
        'foreign_user',
        'revoked',
        'checked_in',
        'missing',
    ],
)
def test_chair_editability_uses_participant_and_core_conditions(app, state):
    with app.test_request_context('/'):
        _set_request_user_and_party(
            authenticated=state != 'anonymous', enabled=state != 'disabled'
        )
        ticket = DbTicket(
            generate_uuid(),
            datetime(2026, 1, 1),
            SimpleNamespace(id=generate_uuid(), party_id=g.party.id),
            'FIXTURE',
            generate_uuid(),
            used_by_id=g.user.id,
            revoked=state == 'revoked',
            user_checked_in=state == 'checked_in',
        )
        if state == 'no_party':
            g.party = None
        elif state == 'foreign_party':
            ticket.party_id = 'other-party'
        elif state == 'foreign_user':
            ticket.used_by_id = generate_uuid()
            ticket.seat_managed_by_id = g.user.id
        elif state == 'missing':
            ticket = None
        assert views.can_edit_chair_information(ticket) is (state == 'eligible')


@pytest.mark.parametrize(
    'state',
    [
        'own',
        'foreign_user',
        'foreign_party',
        'revoked',
        'missing',
        'invalid',
        'no_query',
    ],
)
def test_legacy_redirect_validates_ticket_anchor(app, monkeypatch, state):
    ticket_id = generate_uuid()
    query = '' if state == 'no_query' else f'?ticket_id={ticket_id}'
    if state == 'invalid':
        query = '?ticket_id=invalid'
    with app.test_request_context(f'/{query}'):
        _set_request_user_and_party()
        ticket = SimpleNamespace(
            id=ticket_id,
            party_id=g.party.id,
            used_by_id=g.user.id,
            revoked=state == 'revoked',
            is_used_by=lambda user_id: ticket.used_by_id == user_id,
            is_user_managed_by=lambda _: False,
        )
        if state == 'foreign_user':
            ticket.used_by_id = generate_uuid()
        elif state == 'foreign_party':
            ticket.party_id = 'other-party'
        elif state == 'missing':
            ticket = None
        monkeypatch.setattr(
            views.ticket_service, 'find_ticket', lambda _: ticket
        )
        monkeypatch.setattr(
            views, 'redirect_to', lambda endpoint, **kwargs: (endpoint, kwargs)
        )
        assert views.index() == (
            'ticketing.index_mine',
            {'_anchor': f'ticket-{ticket_id}' if state == 'own' else None},
        )


@pytest.mark.parametrize(
    ('source', 'label'),
    [
        (ChairSource.user, 'Brings own chair'),
        (ChairSource.venue, 'Needs a provided chair'),
        (ChairSource.rental, 'rented'),
        (ChairSource.unknown, 'Not specified yet'),
    ],
)
@pytest.mark.parametrize('checked_in', [False, True])
@pytest.mark.parametrize('rental_enabled', [False, True])
def test_theme_ticket_partial_uses_core_urls_and_displays_all_states(
    source, label, checked_in, rental_enabled
):
    app = Flask(__name__)
    Babel(app, default_locale='en')
    app.register_blueprint(ticketing_views.blueprint, url_prefix='/tickets')
    env = Environment(
        loader=create_site_template_loader(SiteID('totalverplant-36')),
        autoescape=True,
    )
    env.globals.update(
        _=gettext,
        chair_source_label=views.get_chair_source_label,
        can_edit_chair_information=views.can_edit_chair_information,
        is_chair_rental_selection_enabled=lambda _: rental_enabled,
        url_for=url_for,
        render_icon=lambda _: '',
    )

    with app.test_request_context('/'):
        _set_request_user_and_party()
        ticket = SimpleNamespace(
            id=generate_uuid(),
            code='FIXTURE',
            party_id=g.party.id,
            used_by_id=g.user.id,
            revoked=False,
            user_checked_in=checked_in,
            chair_source=source,
            is_used_by=lambda user_id: ticket.used_by_id == user_id,
            is_user_managed_by=lambda _: False,
        )
        html = env.get_template(
            'site/ticketing/_chair_information.html'
        ).render(ticket=ticket, ticket_management_enabled=True)
        ticket.code = 'FIXTURE"<>&'
        escaped_html = env.get_template(
            'site/ticketing/_chair_information.html'
        ).render(ticket=ticket, ticket_management_enabled=True)

    assert label in html
    if checked_in:
        assert 'data-action="set-chair-source"' not in html
    else:
        assert html.count('data-action="set-chair-source"') == (
            3 if rental_enabled else 2
        )
        for offered_source in ['user', 'venue']:
            assert (
                f'/tickets/tickets/{ticket.id}/chair_source/{offered_source}'
                in html
            )
        assert ('/chair_source/rental' in html) is rental_enabled
        assert '/chair_source/unknown' not in html
        assert 'I will bring my own chair' in html
        assert 'I need a provided chair' in html
        assert ('Make selection' in html) is (source is ChairSource.unknown)
    assert f'data-chair-source="{source.name}"' in html
    assert (
        'data-success-text="Chair source of ticket FIXTURE has been set."'
        in html
    )
    assert 'data-refresh-error-text="The save request succeeded,' in html
    assert 'data-conflict-text="The current chair information differs' in html
    assert (
        'data-success-text="Chair source of ticket FIXTURE&#34;&lt;&gt;&amp; has been set."'
        in escaped_html
    )
