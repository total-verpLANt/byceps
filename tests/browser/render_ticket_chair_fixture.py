"""Render real GV36 ticket markup for database-free browser regressions."""

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

from flask import Flask, g
from flask_babel import Babel, gettext, ngettext, pgettext
from jinja2 import ChoiceLoader, DictLoader, FileSystemLoader

from byceps.services.chair_optout.blueprints.site import views as chair_views
from byceps.services.ticketing.blueprints.site import views as ticketing_views
from byceps.services.ticketing.models.ticket import ChairSource
from byceps.util import templatefilters
from byceps.util.framework.flash import flash_success


ROOT = Path(__file__).resolve().parents[2]
SITE = 'totalverplant-36'
TICKET_IDS = [
    UUID('00000000-0000-0000-0000-000000000001'),
    UUID('00000000-0000-0000-0000-000000000002'),
]
BASE = """
<!DOCTYPE html>
<html lang="de"><head><meta name="viewport" content="width=device-width">
<link rel="stylesheet" href="/static/style/common.css">
<link rel="stylesheet" href="/static/style/site.css">
<link rel="stylesheet" href="/static_sites/totalverplant-36/style/totalverplant-36.css">
<link rel="stylesheet" href="/static_sites/totalverplant-36/style/bote-theme.css">
{% block head %}{% endblock %}</head>
<body class="page-ticket"><header class="page-header-fixed">Ticket desk</header>
<main class="main-content"><div class="content-wrapper">
<!-- save-result -->{% block body %}{% endblock %}
</div></main>
<footer class="page-footer-modern" style="min-height:320px">Contact</footer>
<script src="/static/behavior/common.js"></script>
{% block scripts %}{% endblock %}</body></html>
"""


def render_fixtures():
    app = Flask(__name__)
    app.secret_key = 'browser-fixture'
    app.config['BABEL_TRANSLATION_DIRECTORIES'] = str(
        ROOT / 'byceps/translations'
    )
    Babel(app, default_locale='de')
    app.register_blueprint(ticketing_views.blueprint, url_prefix='/tickets')
    app.register_blueprint(chair_views.blueprint, url_prefix='/chair_optout')
    app.add_url_rule('/users/<uuid:user_id>', endpoint='user_profile.view')
    app.jinja_loader = ChoiceLoader(
        [
            DictLoader({'layout/base.html': BASE}),
            FileSystemLoader(ROOT / 'sites' / SITE / 'template_overrides'),
            FileSystemLoader(
                ROOT / 'byceps/services/core/blueprints/common/templates'
            ),
            FileSystemLoader(
                ROOT / 'byceps/services/seating/blueprints/site/templates'
            ),
        ]
    )
    app.jinja_env.globals.update(
        _=gettext,
        ngettext=ngettext,
        pgettext=pgettext,
        render_snippet=lambda *args, **kwargs: '',
        url_for_site_file=lambda filename: f'/static_sites/{SITE}/{filename}',
        chair_source_label=chair_views.get_chair_source_label,
        can_edit_chair_information=chair_views.can_edit_chair_information,
    )
    templatefilters.register(app)
    participant = SimpleNamespace(
        id=UUID('00000000-0000-0000-0000-000000000003'),
        screen_name='Participant',
        authenticated=True,
        deleted=False,
        avatar_url='/static/user_avatar_fallback.svg',
    )
    source_values = {
        'user': ChairSource.user,
        'venue': ChairSource.venue,
        'unknown': ChairSource.unknown,
        'rental': ChairSource.rental,
    }
    pages, pages_rental_off, labels = {}, {}, {}
    with app.test_request_context('/tickets/mine'):
        g.user = participant
        g.party = SimpleNamespace(
            id='fixture-party', ticket_management_enabled=True
        )
        for name, source in source_values.items():
            tickets = [
                SimpleNamespace(
                    id=ticket_id,
                    created_at=datetime(2026, 1, index + 1),
                    party_id=g.party.id,
                    code=f'FIXTURE-{index + 1}',
                    category=SimpleNamespace(title='Standard'),
                    owned_by=participant,
                    owned_by_id=participant.id,
                    used_by=participant,
                    used_by_id=participant.id,
                    user_checked_in=False,
                    revoked=False,
                    order_number=None,
                    occupied_seat=None,
                    chair_source=source if index == 0 else ChairSource.venue,
                    get_seat_manager=lambda: participant,
                    get_user_manager=lambda: participant,
                    is_used_by=lambda user_id: user_id == participant.id,
                    is_user_managed_by=lambda user_id: (
                        user_id == participant.id
                    ),
                )
                for index, ticket_id in enumerate(TICKET_IDS)
            ]
            for rental_enabled, target_pages in [
                (True, pages),
                (False, pages_rental_off),
            ]:
                app.jinja_env.globals['is_chair_rental_selection_enabled'] = (
                    lambda _, enabled=rental_enabled: enabled
                )
                target_pages[name] = {}
                for second_name, second_source in source_values.items():
                    tickets[1].chair_source = second_source
                    target_pages[name][second_name] = (
                        app.jinja_env.get_template(
                            'site/ticketing/index_mine.html'
                        ).render(
                            tickets=tickets,
                            party_title='Fixture party',
                            current_user_uses_any_ticket=True,
                            ticket_management_enabled=True,
                            order_ids_by_order_number={},
                        )
                    )
            labels[name] = chair_views.get_chair_source_label(source)
    with app.test_request_context('/tickets/mine'):
        flash_success(
            gettext(
                'Chair source of ticket %(ticket_code)s has been set.',
                ticket_code='FIXTURE-1',
            )
        )
        notification = app.jinja_env.get_template(
            'layout/_notifications.html'
        ).render()
    return {
        'pages': pages,
        'pagesRentalOff': pages_rental_off,
        'labels': labels,
        'ticketIds': [str(id_) for id_ in TICKET_IDS],
        'notification': notification,
    }


if __name__ == '__main__':
    print(json.dumps(render_fixtures()))
