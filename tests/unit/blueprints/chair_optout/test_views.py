"""
:License: Revised BSD (see `LICENSE` file for details)
"""

import csv
from dataclasses import replace
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from flask import Flask, g, render_template
from flask_babel import Babel, gettext
from jinja2 import ChoiceLoader, DictLoader, FileSystemLoader
from markupsafe import escape
from werkzeug.exceptions import Forbidden

from byceps.services.chair_optout.blueprints.admin import views
from byceps.services.chair_optout.models import ChairOptoutReportEntry
from byceps.services.chair_optout.presentation import get_chair_source_label
from byceps.services.ticketing.models.ticket import ChairSource
from byceps.util import templatefilters
from byceps.util.navigation import Navigation

from tests.helpers import generate_uuid


def _make_entry(code, source, *, has_seat=True):
    return ChairOptoutReportEntry(
        ticket_id=generate_uuid(),
        user_id=generate_uuid(),
        full_name='Alice Example',
        screen_name='alice',
        ticket_code=code,
        seat_id=generate_uuid() if has_seat else None,
        seat_area_slug='main' if has_seat else None,
        seat_label='A-1' if has_seat else None,
        has_seat=has_seat,
        chair_source=source,
    )


def _unwrap(function):
    while hasattr(function, '__wrapped__'):
        function = function.__wrapped__
    return function


def _prepare_report(monkeypatch, entries):
    monkeypatch.setattr(
        views.party_service,
        'find_party',
        lambda _: SimpleNamespace(id='party-1', brand_id='brand-1'),
    )
    monkeypatch.setattr(
        views.chair_optout_service,
        'get_report_entries_for_party',
        lambda _: entries,
    )
    monkeypatch.setattr(
        views.site_service,
        'get_current_sites',
        lambda _: [
            SimpleNamespace(
                id='site-1', party_id='party-1', server_name='www.example.test'
            )
        ],
    )
    monkeypatch.setattr(
        views.party_setting_service, 'find_setting_value', lambda *_: None
    )


def test_admin_views_require_seating_view(app):
    with app.test_request_context('/'):
        g.user = SimpleNamespace(has_permission=lambda _: False)
        for view in (
            views.index,
            views.chair_information,
            views.chair_information_seating_plan,
            views.rental_selection,
            views.export_as_csv,
        ):
            with pytest.raises(Forbidden):
                view('party-1')


@pytest.mark.parametrize(
    ('selected_filter', 'expected_codes'),
    [
        ('all', ['T-1', 'T-2', 'T-3', 'T-4', 'T-5']),
        ('own_chair', ['T-1', 'T-4']),
        ('provided_chair', ['T-2']),
        ('rented_chair', ['T-5']),
        ('not_specified', ['T-3']),
        ('no_seat', ['T-3', 'T-4', 'T-5']),
        ('invalid', ['T-1', 'T-2', 'T-3', 'T-4', 'T-5']),
    ],
)
def test_report_filters_preserve_full_summary(
    app, monkeypatch, selected_filter, expected_codes
):
    entries = [
        _make_entry('T-1', ChairSource.user),
        _make_entry('T-2', ChairSource.venue),
        _make_entry('T-3', ChairSource.unknown, has_seat=False),
        _make_entry('T-4', ChairSource.user, has_seat=False),
        _make_entry('T-5', ChairSource.rental, has_seat=False),
    ]
    _prepare_report(monkeypatch, entries)
    app.config['LOCALE'] = 'en'
    with app.test_request_context(f'/?filter={selected_filter}'):
        context = _unwrap(views.chair_information)('party-1')

    assert context['selected_filter'] == (
        selected_filter if selected_filter != 'invalid' else 'all'
    )
    assert [
        entry.ticket_code for entry in context['report_entries']
    ] == expected_codes
    assert context['summary'].brings_own_chair == 2
    assert context['summary'].needs_provided_chair == 1
    assert context['summary'].rented_chair == 1
    assert context['summary'].not_specified == 1
    assert context['summary'].no_seat == 3


@pytest.mark.parametrize(
    ('selected_filter', 'expected_matches'),
    [
        ('all', {0, 1, 2, 3}),
        ('own_chair', {0}),
        ('provided_chair', {1}),
        ('rented_chair', {2}),
        ('not_specified', {3}),
        ('no_seat', {0, 1, 2, 3}),
    ],
)
def test_plan_uses_compact_query_and_preserves_all_seats(
    app, monkeypatch, selected_filter, expected_matches
):
    sources = {
        0: ChairSource.user,
        1: ChairSource.venue,
        2: ChairSource.rental,
        3: ChairSource.unknown,
    }
    areas = [SimpleNamespace(id='first'), SimpleNamespace(id='second')]
    monkeypatch.setattr(
        views.party_service,
        'find_party',
        lambda _: SimpleNamespace(id='party-1'),
    )
    monkeypatch.setattr(
        views.chair_optout_service,
        'get_chair_sources_for_party',
        lambda _: sources,
    )

    def fail_full_report(_):
        pytest.fail('Plan must not load the full report')

    monkeypatch.setattr(
        views.chair_optout_service,
        'get_report_entries_for_party',
        fail_full_report,
    )
    monkeypatch.setattr(
        views.seating_area_service, 'get_areas_for_party', lambda _: areas
    )
    monkeypatch.setattr(
        views.seat_service,
        'get_area_seats',
        lambda area_id: [f'seat-{area_id}'],
    )
    monkeypatch.setattr(
        views, '_find_seat_stylesheet_site_id', lambda _: 'site-1'
    )
    monkeypatch.setattr(
        views.chair_setting_service,
        'is_rental_selection_enabled',
        lambda _: False,
    )
    with app.test_request_context(f'/?filter={selected_filter}'):
        context = _unwrap(views.chair_information_seating_plan)('party-1')

    assert context['areas_with_seats'] == [
        (areas[0], ['seat-first']),
        (areas[1], ['seat-second']),
    ]
    assert context['chair_sources_by_ticket_id'] == sources
    assert context['matching_ticket_ids'] == expected_matches
    assert context['selected_filter'] == (
        'all' if selected_filter == 'no_seat' else selected_filter
    )
    assert context['show_rental_information'] is True
    assert context['seat_stylesheet_site_id'] == 'site-1'


def test_seat_urls_use_https_and_escape_area_slug():
    entry = replace(
        _make_entry('T-1', ChairSource.user), seat_area_slug='hall /?#'
    )
    no_seat = _make_entry('T-2', ChairSource.unknown, has_seat=False)
    assert views._build_seat_urls_by_ticket_id(
        [entry, no_seat], 'www.example.test'
    ) == {
        entry.ticket_id: f'https://www.example.test/seating/areas/hall%20%2F%3F%23#seat-{entry.seat_id}'
    }
    assert views._build_seat_urls_by_ticket_id([entry], None) == {}


@pytest.mark.parametrize(
    ('primary_site_id', 'expected_server_name'),
    [
        ('preferred', 'z.example.test'),
        (None, None),
        ('unknown', None),
        ('other-party', None),
    ],
)
def test_seat_link_site_selection_with_multiple_sites(
    monkeypatch, primary_site_id, expected_server_name
):
    party = SimpleNamespace(id='party-1', brand_id='brand-1')
    sites = [
        SimpleNamespace(
            id='other-party', party_id='party-2', server_name='a.example.test'
        ),
        SimpleNamespace(
            id='secondary', party_id=party.id, server_name='b.example.test'
        ),
        SimpleNamespace(
            id='preferred', party_id=party.id, server_name='z.example.test'
        ),
    ]
    monkeypatch.setattr(
        views.site_service, 'get_current_sites', lambda _: sites
    )
    monkeypatch.setattr(
        views.party_setting_service,
        'find_setting_value',
        lambda *_: primary_site_id,
    )
    assert views._find_site_server_name_for_party(party) == expected_server_name


def test_unique_current_party_site_is_used(monkeypatch):
    party = SimpleNamespace(id='party-1', brand_id='brand-1')
    monkeypatch.setattr(
        views.site_service,
        'get_current_sites',
        lambda _: [
            SimpleNamespace(
                id='site-1', party_id=party.id, server_name='www.example.test'
            )
        ],
    )
    monkeypatch.setattr(
        views.party_setting_service, 'find_setting_value', lambda *_: None
    )
    assert views._find_site_server_name_for_party(party) == 'www.example.test'


@pytest.mark.parametrize(
    'site_id', ['../outside', 'nested/site', '/absolute', '']
)
def test_stylesheet_rejects_noncanonical_site_ids(
    monkeypatch, tmp_path, site_id
):
    monkeypatch.setattr(views, 'SITES_PATH', tmp_path)
    assert views._seat_stylesheet_exists(site_id) is False


def test_stylesheet_rejects_symlink_outside_sites(monkeypatch, tmp_path):
    sites_path = tmp_path / 'sites'
    sites_path.mkdir()
    outside = tmp_path / 'outside'
    stylesheet = outside / 'static/style/seating.css'
    stylesheet.parent.mkdir(parents=True)
    stylesheet.touch()
    (sites_path / 'linked').symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(views, 'SITES_PATH', sites_path)
    assert views._seat_stylesheet_exists('linked') is False


@pytest.mark.parametrize(
    ('primary', 'site_ids', 'expected'),
    [
        ('first', ['first', 'second'], 'first'),
        (None, ['first'], 'first'),
        (None, ['first', 'second'], None),
        ('missing', ['first'], None),
    ],
)
def test_stylesheet_selection(
    monkeypatch, tmp_path, primary, site_ids, expected
):
    for site_id in site_ids:
        stylesheet = tmp_path / site_id / 'static/style/seating.css'
        stylesheet.parent.mkdir(parents=True)
        stylesheet.touch()
    monkeypatch.setattr(views, 'SITES_PATH', tmp_path)
    monkeypatch.setattr(
        views.party_setting_service, 'find_setting_value', lambda *_: primary
    )
    monkeypatch.setattr(
        views.site_service,
        'get_all_sites',
        lambda: [
            SimpleNamespace(id=site_id, party_id='party-1')
            for site_id in site_ids
        ],
    )
    assert views._find_seat_stylesheet_site_id('party-1') == expected


def _export(app, monkeypatch, entries):
    _prepare_report(monkeypatch, entries)
    with app.test_request_context('/'):
        g.user = SimpleNamespace(
            has_permission=lambda permission: permission == 'seating.view'
        )
        response = views.export_as_csv('party-1')
    return list(csv.reader(StringIO(response.get_data(as_text=True))))


def test_export_includes_distinct_sources_and_no_seat(app, monkeypatch):
    entries = [
        _make_entry('T-1', ChairSource.user),
        _make_entry('T-2', ChairSource.venue),
        _make_entry('T-3', ChairSource.rental),
        _make_entry('T-4', ChairSource.unknown, has_seat=False),
    ]
    rows = _export(app, monkeypatch, entries)
    assert rows[0] == [
        'Name',
        'Nickname',
        'Ticket number',
        'Seat label',
        'Chair information',
    ]
    assert [row[-1] for row in rows[1:]] == [
        'Brings own chair',
        'Needs a provided chair',
        'rented',
        'Not specified yet',
    ]
    assert rows[-1][3] == 'no seat'


@pytest.mark.parametrize('prefix', ['=', '+', '-', '@', '\t', '\r'])
def test_export_escapes_all_formula_cells(app, monkeypatch, prefix):
    entry = replace(
        _make_entry(f'{prefix}ticket', ChairSource.user),
        full_name=f'{prefix}HYPERLINK("https://example.test", "x")',
        screen_name=f'{prefix}nickname',
        seat_label=f'{prefix}seat',
    )
    row = _export(app, monkeypatch, [entry])[1]
    assert row[:4] == [
        f"'{entry.full_name}",
        f"'{entry.screen_name}",
        f"'{entry.ticket_code}",
        f"'{entry.seat_label}",
    ]


@pytest.mark.parametrize('label', [None, ''])
def test_export_distinguishes_unnamed_seat_from_no_seat(
    app, monkeypatch, label
):
    rows = _export(
        app,
        monkeypatch,
        [
            replace(_make_entry('T-1', ChairSource.user), seat_label=label),
            _make_entry('T-2', ChairSource.venue, has_seat=False),
        ],
    )
    assert rows[1][3] == 'unnamed'
    assert rows[2][3] == 'no seat'


@pytest.mark.parametrize(
    ('source', 'label'),
    [
        (ChairSource.user, 'Brings own chair'),
        (ChairSource.venue, 'Needs a provided chair'),
        (ChairSource.rental, 'rented'),
        (ChairSource.unknown, 'Not specified yet'),
    ],
)
def test_shared_chair_source_label(app, source, label):
    with app.test_request_context('/'):
        assert get_chair_source_label(source) == label


@pytest.fixture
def chair_template_app():
    """Load the real templates and macros without app initialization or a DB."""
    app = Flask(__name__)
    Babel(app)
    app.register_blueprint(views.blueprint, url_prefix='/chair_optout')
    app.add_url_rule(
        '/static_sites/<site_id>/<path:filename>', endpoint='site_file'
    )
    root = Path(views.__file__).parents[4]
    app.jinja_loader = ChoiceLoader(
        [
            DictLoader(
                {
                    'layout/admin/base.html': '{% block head %}{% endblock %}'
                    '{% block body %}{% endblock %}'
                    '{% block scripts %}{% endblock %}'
                }
            ),
            FileSystemLoader(
                [
                    root / 'services/chair_optout/blueprints/admin/templates',
                    root / 'services/core/blueprints/admin/templates',
                    root / 'services/core/blueprints/common/templates',
                ]
            ),
        ]
    )
    app.jinja_env.globals.update(_=gettext)

    @app.context_processor
    def inject_navigation():
        return {'Navigation': Navigation}

    templatefilters.register(app)
    return app


@pytest.fixture
def render_plan(chair_template_app):
    app = chair_template_app

    def render(sources, *, selected_filter='all', rental_enabled=False):
        seats = [
            SimpleNamespace(
                id=ticket_id,
                coord_x=10 * ticket_id,
                coord_y=12,
                label='<img src=x onerror="alert(1)">',
                type_='narrow',
                rotation=45,
                occupied=True,
                occupied_by_ticket_id=ticket_id,
                occupied_by_user=SimpleNamespace(
                    avatar_url='/avatar.png', screen_name='<b>Alice</b>'
                ),
            )
            for ticket_id in sources
        ]
        seats.append(
            SimpleNamespace(
                id=99,
                coord_x=99,
                coord_y=12,
                label=None,
                type_=None,
                rotation=0,
                occupied=False,
                occupied_by_ticket_id=None,
            )
        )
        area = SimpleNamespace(
            title='Main hall',
            party_id='party-1',
            image_filename='hall.png',
            image_height=200,
            image_width=300,
        )
        with app.test_request_context(
            '/chair_optout/for_party/party-1/chair_information/seating_plan'
        ):
            return render_template(
                'admin/chair_optout/seating_plan.html',
                party=SimpleNamespace(id='party-1', title='Party'),
                areas_with_seats=[(area, seats)],
                chair_sources_by_ticket_id=sources,
                matching_ticket_ids={
                    ticket_id
                    for ticket_id, source in sources.items()
                    if views._matches_filter(source, True, selected_filter)
                },
                show_rental_information=(
                    rental_enabled or ChairSource.rental in sources.values()
                ),
                selected_filter=selected_filter,
                seat_stylesheet_site_id='site-1',
            )

    return render


def test_plan_renders_seat_specific_markers_tooltips_and_geometry(render_plan):
    html = render_plan(
        {
            1: ChairSource.user,
            2: ChairSource.venue,
            3: ChairSource.rental,
            4: ChairSource.unknown,
        },
        selected_filter='provided_chair',
    )
    markers = {
        1: ('seat--own-chair', 'Brings own chair'),
        2: ('seat--chair-venue', 'Needs a provided chair'),
        3: ('seat--chair-rental', 'Rental chair'),
        4: ('seat--chair-unknown', 'Not specified yet'),
    }
    for seat_id, (marker, note) in markers.items():
        markup = html.split(f'id="seat-{seat_id}"', 1)[1].split('</div>', 1)[0]
        assert marker in markup
        assert f'data-tooltip-note="{note}"' in markup
        assert ('seat--filter-dimmed' in markup) is (seat_id != 2)
        assert 'seat-type--narrow' in markup
        assert f'left: {10 * seat_id}px; top: 12px;' in markup
        assert 'rotate(45deg)' in markup
        assert f'data-occupier-name="{escape("<b>Alice</b>")}"' in markup
        label = '<img src=x onerror="alert(1)">'
        assert f'data-label="{escape(label)}"' in markup
    free_seat = html.split('id="seat-99"', 1)[1].split('</div>', 1)[0]
    assert 'data-tooltip-note' not in free_seat
    assert 'data-label="unnamed"' in free_seat
    assert 'seat--filter-dimmed' in free_seat
    assert 'height: 200px; width: 300px;' in html
    assert (
        html.index('/static/style/seating.css')
        < html.index('/static_sites/site-1/style/seating.css')
        < html.index('style/chair_optout.css')
    )
    assert 'behavior/chair_optout.js' in html
    assert 'behavior/seating.js' not in html
    assert 'filter=rented_chair' in html


def test_plan_hides_rental_filter_and_legend_without_rentals(render_plan):
    html = render_plan({1: ChairSource.user, 2: ChairSource.unknown})
    assert 'filter=rented_chair' not in html
    assert 'seat--chair-rental' not in html
    assert 'seat--filter-dimmed' not in html
    assert 'filter=no_seat' not in html


@pytest.mark.parametrize('has_rental', [False, True])
@pytest.mark.parametrize('rental_enabled', [False, True])
def test_report_rental_card_and_filter_use_unfiltered_records(
    chair_template_app, has_rental, rental_enabled
):
    entries = [_make_entry('T-1', ChairSource.user)]
    if has_rental:
        entries.append(_make_entry('T-2', ChairSource.rental))
    summary = views.chair_optout_service.summarize_report_entries(entries)
    with chair_template_app.test_request_context(
        '/chair_optout/for_party/party-1/chair_information'
    ):
        g.user = SimpleNamespace(has_permission=lambda _: False)
        html = render_template(
            'admin/chair_optout/index.html',
            party=SimpleNamespace(id='party-1', title='Party'),
            report_entries=[],
            summary=summary,
            selected_filter='no_seat',
            seat_urls_by_ticket_id={},
            show_rental_information=rental_enabled or has_rental,
        )
    assert ('<figcaption>Rental chair</figcaption>' in html) is has_rental
    assert ('filter=rented_chair' in html) is (rental_enabled or has_rental)
    assert 'name="enabled"' not in html
    assert 'Existing rental information' not in html
    assert 'filter=no_seat' in html
    assert 'chair_information/seating_plan?filter=all' in html
    assert 'chair_information/seating_plan?filter=no_seat' not in html


@pytest.mark.parametrize('has_rental', [False, True])
@pytest.mark.parametrize('rental_enabled', [False, True])
def test_plan_rental_visibility_matrix(render_plan, has_rental, rental_enabled):
    sources = {1: ChairSource.user}
    if has_rental:
        sources[2] = ChairSource.rental
    html = render_plan(
        sources, rental_enabled=rental_enabled, selected_filter='rented_chair'
    )
    visible = rental_enabled or has_rental
    filters = html.split('<nav class="main-tabs">', 1)[1].split('</nav>', 1)[0]
    assert ('filter=rented_chair' in filters) is visible
    assert ('seat--chair-rental" aria-hidden="true"' in html) is visible
    assert ('data-chair-source="rental"' in html) is has_rental
    assert 'seat--filter-dimmed' in html
