"""Serve the real admin app with disposable data for browser acceptance tests.

Requires POSTGRES_DB=byceps_chair_followup_browser and isolated PostgreSQL/Redis.
The database is recreated. Pass a fixture JSON path under the local runtime.
"""

import json
import os
from pathlib import Path
import sys

from flask import Flask, g
from flask_babel import gettext
from PIL import Image
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError
from werkzeug.serving import run_simple

from byceps.application import _assemble_configuration, create_admin_app
from byceps.config.converter import assemble_database_uri
from byceps.config.models import AdminWebAppConfig, DatabaseConfig, RedisConfig
from byceps.database import db
from byceps.services.authz import authz_service
from byceps.services.authz.models import PermissionID, RoleID
from byceps.services.brand import brand_service
from byceps.services.chair_optout import chair_setting_service
from byceps.services.party import party_setting_service
from byceps.services.party.models import PartyID
from byceps.services.seating import seat_service, seating_area_service
from byceps.services.ticketing import (
    ticket_category_service,
    ticket_creation_service,
    ticket_seat_management_service,
)
from byceps.services.ticketing.models.ticket import ChairSource

from tests.helpers import (
    create_party,
    create_role_with_permissions_assigned,
    create_user,
    http_client,
    log_in_user,
)
from tests.integration.conftest import build_byceps_config
from tests.integration.database import populate_database, set_up_database


def _validate_resource_targets(config, database_uri, redis_url) -> None:
    for key, expected in [
        ('SQLALCHEMY_DATABASE_URI', database_uri),
        ('REDIS_URL', redis_url),
    ]:
        try:
            matches = make_url(config[key]) == make_url(expected)
        except (ArgumentError, KeyError, TypeError, ValueError):
            matches = False
        if not matches:
            # Never include URLs: they may contain inherited credentials.
            raise RuntimeError(
                f'{key} must match the explicit isolated browser fixture target.'
            ) from None


def create_fixture_app(
    data_path: Path, database_config: DatabaseConfig, redis_config: RedisConfig
):
    byceps_config = build_byceps_config(
        data_path, database_config, redis_config
    )
    app_config = AdminWebAppConfig(server_name='127.0.0.1:58080')
    database_uri = assemble_database_uri(database_config)
    # Assemble the same effective configuration as Core, before initializing
    # clients or recreating tables. Environment overrides remain enabled.
    config = _assemble_configuration(byceps_config, app_config)
    _validate_resource_targets(config, database_uri, redis_config.url)
    app = create_admin_app(byceps_config, app_config)
    _validate_resource_targets(app.config, database_uri, redis_config.url)

    bootstrap = Flask(__name__)
    bootstrap.config['SQLALCHEMY_DATABASE_URI'] = database_uri
    db.init_app(bootstrap)
    with bootstrap.app_context():
        set_up_database()
        populate_database()
    return app


def serve(fixture_path: Path) -> None:
    if os.environ.get('POSTGRES_DB') != 'byceps_chair_followup_browser':
        raise RuntimeError('A dedicated browser test database is required.')
    database_config = DatabaseConfig(
        host=os.environ['POSTGRES_HOST'],
        port=int(os.environ['POSTGRES_PORT']),
        username=os.environ['POSTGRES_USER'],
        password=os.environ['POSTGRES_PASSWORD'],
        database=os.environ['POSTGRES_DB'],
    )
    redis_config = RedisConfig(
        url=f'redis://{os.environ["REDIS_HOST"]}:{os.environ["REDIS_PORT"]}/1'
    )
    app = create_fixture_app(fixture_path.parent, database_config, redis_config)
    app.config['SESSION_COOKIE_SECURE'] = False
    with app.app_context():
        brand = brand_service.create_brand('chair-browser', 'Chair browser')
        participant = create_user('ChairParticipant')
        cookies = {}
        for name, permissions in [
            ('writer', {'admin.access', 'seating.view', 'party.update'}),
            ('reader', {'admin.access', 'seating.view'}),
        ]:
            user = create_user(name)
            role_id = RoleID(f'chair-browser-{name}')
            create_role_with_permissions_assigned(
                role_id, [PermissionID(p) for p in permissions]
            )
            authz_service.assign_role_to_user(role_id, user)
            log_in_user(user.id)
            with http_client(app, user_id=user.id) as client:
                cookies[name] = client.get_cookie(
                    'session', domain='127.0.0.1'
                ).value

        parties = {}
        for name, enabled, has_rental in [
            ('off-empty', False, False),
            ('on-empty', True, False),
            ('on-rental', True, True),
            ('off-rental', False, True),
        ]:
            party = create_party(brand, PartyID(name), f'Browser {name}')
            party_setting_service.create_or_update_setting(
                party.id, 'primary_party_site_id', 'totalverplant-36'
            )
            category = ticket_category_service.create_category(
                party.id, 'Standard'
            )
            area = seating_area_service.create_area(
                party.id,
                'main',
                'Main hall',
                image_filename='hall.png',
                image_width=640,
                image_height=240,
            )
            seat_ids = {}
            for index, source in enumerate(
                [
                    ChairSource.rental if has_rental else ChairSource.unknown,
                    ChairSource.user,
                    ChairSource.venue,
                ]
            ):
                seat = seat_service.create_seat(
                    area.id,
                    80 + index * 80,
                    100,
                    category.id,
                    label=f'A-{index + 1}',
                    rotation=45 if index == 0 else 0,
                )
                ticket = ticket_creation_service.create_ticket(
                    category, participant, user=participant
                )
                ticket_seat_management_service.occupy_seat(
                    ticket.id, seat.id, participant
                ).unwrap()
                ticket_seat_management_service.set_chair_source(
                    ticket.id, source, participant
                ).unwrap()
                seat_ids[source.name] = str(seat.id)
            chair_setting_service.set_rental_selection_enabled(
                party.id, enabled
            )
            parties[name] = {'id': party.id, 'seats': seat_ids}

        g.user = None
        fixture_path.write_text(
            json.dumps(
                {
                    'baseUrl': 'http://127.0.0.1:58080',
                    'cookies': cookies,
                    'parties': parties,
                    'labels': {
                        message: gettext(message)
                        for message in [
                            'Participant list',
                            'Graphical seating plan',
                            'Rental chair selection',
                            'Rental chair',
                            'Are you sure you want to enable this for the party?',
                        ]
                    },
                }
            )
        )
    background = fixture_path.parent / 'hall.png'
    Image.new('RGB', (640, 240), '#111111').save(background)
    root = Path(__file__).resolve().parents[2]
    static_files = {
        '/static_sites/totalverplant-36': str(
            root / 'sites/totalverplant-36/static'
        ),
        **{
            f'/data/parties/{party_id}/seating/areas/hall.png': str(background)
            for party_id in parties
        },
    }
    run_simple(
        '127.0.0.1', 58080, app, threaded=True, static_files=static_files
    )


if __name__ == '__main__':
    serve(Path(sys.argv[1]).resolve())
