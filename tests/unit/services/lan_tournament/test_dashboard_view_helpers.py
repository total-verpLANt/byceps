from dataclasses import replace
from datetime import datetime, timedelta, UTC
import functools
import json
from pathlib import Path
import re
from typing import Any
from urllib.parse import parse_qs, parse_qsl, urlsplit

from babel.messages.extract import extract_from_file
from babel.messages.pofile import read_po
from flask import Blueprint, Flask
from flask_babel import Babel, force_locale
import pytest
from werkzeug.datastructures import MultiDict

from byceps.services.lan_tournament import (
    dashboard_view_helpers as helpers,
    tournament_dashboard_coordination_service as coordination,
    tournament_dashboard_service as dashboard_service,
)
from byceps.services.lan_tournament.dashboard_view_helpers import (
    build_dashboard_context,
    build_dashboard_list_url,
    build_dashboard_return,
    dashboard_labels,
    format_active_duration,
    format_wall_time,
    parse_dashboard_query,
    parse_dashboard_return,
    serialize_dashboard_error,
    serialize_dashboard_fragment,
    serialize_dashboard_success,
)
from byceps.services.lan_tournament.models.bracket import Bracket
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.operational_timing import (
    TrafficTier,
)
from byceps.services.lan_tournament.models.tournament import TournamentID
from byceps.services.lan_tournament.models.tournament_dashboard import (
    AckUnavailableReason,
    DashboardAcknowledgementSummary,
    DashboardConflict,
    DashboardConflictRef,
    DashboardMatchLocation,
    DashboardNonActionableCounts,
    DashboardPage,
    DashboardQuery,
    DashboardRow,
    DashboardRowState,
    DashboardSettings,
    DashboardStatusNote,
    DashboardTierCounts,
    DashboardTournamentRef,
)
from byceps.services.lan_tournament.models.tournament_match import (
    TournamentMatchID,
)
from byceps.services.user.models import UserID
from byceps.util.uuid import generate_uuid7


ROOT = Path(__file__).resolve().parents[4]
MODULE = ROOT / 'byceps/services/lan_tournament/dashboard_view_helpers.py'
PO = ROOT / 'byceps/translations/de/LC_MESSAGES/messages.po'

PARTY = 'pixelnacht-36'
# 14:00 in the party's time zone (CEST, UTC+2).
NOW = datetime(2026, 10, 8, 12, 0, 0)
MINUTE = 60_000_000

_PLAIN_TYPES = (str, int, bool, type(None))
_PLACEHOLDER = re.compile(r'%\((\w+)\)\d*[sd]')


@pytest.fixture(scope='module')
def app():
    app = Flask(__name__)
    app.config['TESTING'] = True
    app.config['SECRET_KEY'] = 'dashboard-view-helpers-unit-test-only'
    app.config['BABEL_DEFAULT_LOCALE'] = 'en'
    app.config['BABEL_DEFAULT_TIMEZONE'] = 'UTC'
    app.config['TIMEZONE'] = 'Europe/Berlin'
    Babel(app)

    def stub(**_):
        return ''

    admin = Blueprint('lan_tournament_admin', 'admin')
    for rule, endpoint in [
        ('/for_party/<party_id>/dashboard', 'dashboard_for_party'),
        ('/for_party/<party_id>/dashboard/poll', 'dashboard_poll_for_party'),
        (
            '/for_party/<party_id>/dashboard/matches/<match_id>/pin',
            'dashboard_pin',
        ),
        (
            '/for_party/<party_id>/dashboard/matches/<match_id>/ack',
            'dashboard_ack',
        ),
        ('/matches/<match_id>', 'view_match'),
        ('/tournaments/<tournament_id>', 'view'),
    ]:
        admin.add_url_rule(rule, endpoint=endpoint, view_func=stub)
    app.register_blueprint(admin, url_prefix='/lan-tournaments')

    site = Blueprint('lan_tournament', 'site')
    for rule, endpoint in [
        ('/', 'index'),
        ('/orga-dashboard', 'orga_dashboard'),
        ('/orga-dashboard/poll', 'orga_dashboard_poll'),
        ('/orga-dashboard/matches/<match_id>/pin', 'orga_dashboard_pin'),
        ('/orga-dashboard/matches/<match_id>/ack', 'orga_dashboard_ack'),
        ('/matches/<match_id>', 'view_match'),
        ('/<tournament_id>', 'view'),
    ]:
        site.add_url_rule(rule, endpoint=endpoint, view_func=stub)
    app.register_blueprint(site, url_prefix='/lan-tournaments')

    return app


@pytest.fixture(autouse=True)
def request_context(app):
    with app.test_request_context('/'):
        yield


def _match_id() -> TournamentMatchID:
    return TournamentMatchID(generate_uuid7())


def _tournament_id() -> TournamentID:
    return TournamentID(generate_uuid7())


def _settings(yellow=15, red=45, poll=30) -> DashboardSettings:
    return DashboardSettings(
        yellow_minutes=yellow,
        red_minutes=red,
        poll_seconds=poll,
        page_size=50,
        threshold_source='deployment',
    )


def _query(**fields) -> DashboardQuery:
    return DashboardQuery(per_page=50, **fields)


def _row(**fields) -> DashboardRow:
    defaults: dict[str, Any] = dict(
        match_id=_match_id(),
        tournament_id=_tournament_id(),
        tournament_name='Kupfer-Cup',
        game='Arena Five',
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        location=DashboardMatchLocation(
            phase=1, bracket=Bracket.WINNERS, round=1, match_order=2
        ),
        contestant_names=('Kupferfüchse', 'Nachtbus'),
        orga_names=('Mara', 'Jonas'),
        state=DashboardRowState.DUE,
        tier=TrafficTier.GREEN,
        created_at=NOW - timedelta(hours=1),
        occupied_since=NOW - timedelta(minutes=40),
        episode_opened_at=NOW - timedelta(minutes=30),
        total_active_wait_us=30 * MINUTE,
        alert_interval_us=10 * MINUTE,
        last_changed_at=NOW - timedelta(minutes=20),
        readiness_available=True,
        episode_id=generate_uuid7(),
        ack_revision=0,
        ack_unavailable_reason=AckUnavailableReason.BELOW_THRESHOLD,
    )
    defaults.update(fields)
    return DashboardRow(**defaults)


def _page(rows=(), **fields) -> DashboardPage:
    rows = tuple(rows)
    defaults: dict[str, Any] = dict(
        rows=rows,
        as_of=NOW,
        total_count=len(rows),
        page=1,
        per_page=50,
        total_pages=1 if rows else 0,
        tier_counts=DashboardTierCounts(),
        non_actionable_counts=DashboardNonActionableCounts(),
    )
    defaults.update(fields)
    return DashboardPage(**defaults)


def _context(
    page=None,
    query=None,
    settings=None,
    surface='admin',
    errors=None,
    token='token-1',
):
    return build_dashboard_context(
        page if page is not None else _page(),
        query or _query(),
        settings or _settings(),
        surface=surface,
        csrf_token=token,
        party_id=PARTY if surface == 'admin' else None,
        query_errors=errors,
    )


def _rows(context) -> list[dict]:
    return context['rows']


def _walk(value, path='context'):
    """Fail on anything but plain data (dict, list, str, int, bool, None)."""
    if isinstance(value, dict):
        for key, item in value.items():
            assert type(key) is str, (path, key)
            _walk(item, f'{path}.{key}')
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _walk(item, f'{path}[{index}]')
    else:
        assert type(value) in _PLAIN_TYPES, (path, type(value))


# ---------------------------------------------------------- phase labels


def _playoff(bracket, round_, order, *, phase=2):
    return _row(
        location=DashboardMatchLocation(
            phase=phase, bracket=bracket, round=round_, match_order=order
        )
    )


def test_page_safe_labels_preserve_phase_round_context():
    # One playoff bracket of four rounds: the last stored round is 3.
    bracket = [
        _playoff(Bracket.WINNERS, 0, 0),
        _playoff(Bracket.WINNERS, 1, 1),
        _playoff(Bracket.WINNERS, 2, 0),  # the semifinal by position
        _playoff(Bracket.WINNERS, 3, 0),  # the final by position
    ]
    whole = {
        row.match_id: r['location']
        for row, r in zip(bracket, _rows(_context(_page(bracket))), strict=True)
    }

    # A page break must not rename anything: each half and each single
    # match reads exactly as it does on the whole page.
    for chunk in (bracket[:2], bracket[2:], [bracket[2]], [bracket[3]]):
        labels = {
            row.match_id: r['location']
            for row, r in zip(chunk, _rows(_context(_page(chunk))), strict=True)
        }
        assert labels == {mid: whole[mid] for mid in labels}

    # Stored positions are zero-based; the text counts from one. A late
    # round is never called a final or a semifinal: that needs the whole
    # phase, which a page does not have.
    assert list(whole.values()) == [
        'Playoffs · Winners bracket · Round 1 · Game 1',
        'Playoffs · Winners bracket · Round 2 · Game 2',
        'Playoffs · Winners bracket · Round 3 · Game 1',
        'Playoffs · Winners bracket · Round 4 · Game 1',
    ]
    for text in whole.values():
        assert not re.search(r'final|semi|quarter', text, re.IGNORECASE)

    label = helpers.build_match_location_label
    assert label(
        DashboardMatchLocation(phase=1, group_order=2, round=1, match_order=3)
    ) == ('Group C · Round 2 · Game 4')
    assert label(DashboardMatchLocation(phase=1, round=0, match_order=0)) == (
        'Round 1 · Game 1'
    )
    assert label(
        DashboardMatchLocation(
            phase=1, bracket=Bracket.LOSERS, round=4, match_order=1
        )
    ) == ('Losers bracket · Round 5 · Game 2')
    # The grand final and the third place have one round: none is shown.
    assert label(
        DashboardMatchLocation(
            phase=2, bracket=Bracket.GRAND_FINAL, round=0, match_order=0
        )
    ) == ('Playoffs · Grand final · Game 1')
    assert label(
        DashboardMatchLocation(
            phase=2, bracket=Bracket.THIRD_PLACE, round=0, match_order=0
        )
    ) == ('Playoffs · Third place · Game 1')
    # A lobby is a lobby, and its group index is not a group.
    assert (
        label(
            DashboardMatchLocation(
                phase=1, group_order=1, round=0, match_order=1
            ),
            game_format=GameFormat.FREE_FOR_ALL,
        )
        == 'Round 1 · Lobby 2'
    )
    assert (
        label(
            DashboardMatchLocation(
                phase=2, bracket=Bracket.WINNERS, round=2, match_order=0
            ),
            game_format=GameFormat.FREE_FOR_ALL,
        )
        == 'Playoffs · Winners pool · Round 3 · Lobby 1'
    )

    # A conflict counterpart reads like the same match listed on a page.
    ref = DashboardConflictRef(
        match_id=_match_id(),
        tournament_id=_tournament_id(),
        tournament_name='Neon-Duell',
        location=bracket[2].location,
        contestant_names=('Nori', 'Komet'),
    )
    block = _rows(
        _context(
            _page(
                [
                    _row(
                        conflicts=(
                            DashboardConflict(
                                user_id=generate_uuid7(),
                                user_display_name='Nori',
                                visible_refs=(ref,),
                            ),
                        )
                    )
                ]
            )
        )
    )[0]['conflicts'][0]
    assert block['refs'][0]['location'] == whole[bracket[2].match_id]
    assert block['refs'][0]['text'] == (
        'Neon-Duell · Playoffs · Winners bracket · Round 3 · Game 1:'
        ' Nori against Komet'
    )


# ----------------------------------------------------------- conflicts


def _conflict(name, *, via=None, refs=(), external=False) -> DashboardConflict:
    return DashboardConflict(
        user_id=UserID(generate_uuid7()),
        user_display_name=name,
        via_team_name=via,
        visible_refs=tuple(refs),
        has_external_conflict=external,
    )


def _ref(
    tournament='Neon-Duell', *, via=None, list_page=None, game_format=None
):
    return DashboardConflictRef(
        match_id=_match_id(),
        tournament_id=_tournament_id(),
        tournament_name=tournament,
        location=DashboardMatchLocation(
            phase=1, group_order=0, round=0, match_order=1
        ),
        contestant_names=('Nori', 'Komet'),
        via_team_name=via,
        list_page=list_page,
        game_format=game_format,
    )


def test_context_contains_no_hidden_conflict_objects():
    ref_on_page_2 = _ref(via='Kupferfüchse', list_page=2)
    ref_filtered_out = _ref('Orbit-Lobby', list_page=None)
    row = _row(
        conflicts=(
            _conflict(
                'Nori',
                via='Kupferfüchse',
                refs=(ref_on_page_2, ref_filtered_out),
                external=True,
            ),
            _conflict('Tess', via='Geheimteam', external=True),
        )
    )
    context = _context(_page([row], total_pages=2, total_count=60))

    # Plain data all the way down: no DTO, enum, UUID or lazy string.
    _walk(context)
    json.dumps(context)

    blocks = _rows(context)[0]['conflicts']
    assert [(b['kind'], b['name']) for b in blocks] == [
        ('authorized', 'Nori'),
        ('external', 'Nori'),
        ('external', 'Tess'),
    ]

    authorized, external_of_nori, external_only = blocks
    assert authorized['role'] == '(member of Kupferfüchse)'
    assert authorized['text'] == (
        'Nori (member of Kupferfüchse) is needed in another match at the'
        ' same time:'
    )
    assert [r['list_page'] for r in authorized['refs']] == [2, None]

    # The counterpart on another page links to that page of the same list,
    # at its row; a filtered-out counterpart has only the match link.
    on_page, filtered = authorized['refs']
    assert on_page['list_page_label'] == 'In this list: page 2'
    split = urlsplit(on_page['list_page_url'])
    assert split.path == f'/lan-tournaments/for_party/{PARTY}/dashboard'
    assert parse_qs(split.query) == {'page': ['2']}
    assert split.fragment == f'lt-row-{ref_on_page_2.match_id}'
    assert filtered['list_page_label'] is None
    assert filtered['list_page_url'] is None
    assert filtered['via'] is None
    assert on_page['via'] == 'as member of Kupferfüchse'
    assert on_page['text'].endswith(' – as member of Kupferfüchse')
    assert urlsplit(on_page['match_url']).path == (
        f'/lan-tournaments/matches/{ref_on_page_2.match_id}'
    )

    # The external hint is a name and one fixed sentence: no team (the
    # team of the listed row stays out), no reference, no count.
    expected = {
        'kind': 'external',
        'role': None,
        'tail': (
            'is needed in a match outside your tournaments at the same time.'
        ),
        'refs': [],
    }
    for block in (external_of_nori, external_only):
        assert {
            k: v for k, v in block.items() if k not in ('name', 'text')
        } == expected
        assert block['text'] == f'{block["name"]} {expected["tail"]}'
    assert 'Geheimteam' not in json.dumps(external_only)

    # The same overlap with other hidden facts renders the same bytes: the
    # DTO carries one boolean, whatever the hidden multiplicity.
    def external_of(person):
        ctx = _context(_page([_row(conflicts=(person,))]))
        return _rows(ctx)[0]['conflicts']

    assert external_of(_conflict('Tess', via='A', external=True)) == (
        external_of(_conflict('Tess', via='B', external=True))
    )
    assert external_of(_conflict('Tess', external=True)) == (
        external_of(_conflict('Tess', via='Geheimteam', external=True))
    )

    # Every row state yields plain data, never a repository object.
    for state in DashboardRowState:
        _walk(_context(_page([_row(state=state, tier=None)])))


def test_a_lobby_counterpart_reads_like_the_lobby_row_itself():
    location = DashboardMatchLocation(
        phase=1, group_order=1, round=0, match_order=1
    )
    lobby = DashboardConflictRef(
        match_id=_match_id(),
        tournament_id=_tournament_id(),
        tournament_name='Orbit-Lobby',
        location=location,
        contestant_names=('Tess', 'Echo'),
        game_format=GameFormat.FREE_FOR_ALL,
    )
    pairing = replace(lobby, game_format=GameFormat.ONE_V_ONE)
    unknown = replace(lobby, game_format=None)

    def ref_of(ref):
        row = _row(
            conflicts=(
                DashboardConflict(
                    user_id=UserID(generate_uuid7()),
                    user_display_name='Nori',
                    visible_refs=(ref,),
                ),
            )
        )
        return _rows(_context(_page([row])))[0]['conflicts'][0]['refs'][0]

    # The row of the same lobby, as it is listed.
    row_location = _rows(
        _context(
            _page(
                [
                    _row(
                        game_format=GameFormat.FREE_FOR_ALL,
                        location=location,
                        contestant_names=('Tess', 'Echo'),
                    )
                ]
            )
        )
    )[0]['location']
    assert row_location == 'Round 1 · Lobby 2'

    shown = ref_of(lobby)
    assert shown['location'] == row_location
    # Two entrants of a lobby are not "against" each other.
    assert shown['text'] == 'Orbit-Lobby · Round 1 · Lobby 2: Tess, Echo'

    assert ref_of(pairing)['location'] == 'Group B · Round 1 · Game 2'
    assert ref_of(pairing)['text'] == (
        'Orbit-Lobby · Group B · Round 1 · Game 2: Tess against Echo'
    )
    # Without the format the ref reads as a pairing, as before.
    assert ref_of(unknown)['location'] == 'Group B · Round 1 · Game 2'


# -------------------------------------------------------------- times


def test_original_and_operational_times_have_distinct_labels():
    row = _row(
        state=DashboardRowState.DUE,
        tier=TrafficTier.YELLOW,
        created_at=datetime(2026, 10, 8, 9, 0),
        occupied_since=datetime(2026, 10, 8, 9, 10),
        episode_opened_at=datetime(2026, 10, 8, 9, 30),
        last_changed_at=datetime(2026, 10, 8, 9, 45),
        total_active_wait_us=30 * MINUTE,
        alert_interval_us=15 * MINUTE,
    )
    shown = _rows(_context(_page([row])))[0]

    times = shown['times']
    assert [t['key'] for t in times] == [
        'wait',
        'occupied',
        'due',
        'changed',
        'created',
    ]
    assert [t['label'] for t in times] == [
        'Total active wait',
        'Occupied since',
        'Due since (this episode)',
        'Last match change',
        'Created',
    ]
    assert len({t['label'] for t in times}) == 5
    # Party time: UTC+2. Each value is its own fact.
    assert [t['value'] for t in times] == [
        '30 min',
        '11:10',
        '11:30',
        '11:45',
        '11:00 · age 3 h 00 min',
    ]
    assert [t['is_key'] for t in times] == [True, False, False, False, False]

    # The alert interval is the tier cell's own value, not a sixth time,
    # and it differs from the total wait.
    assert shown['tier']['value'] == '15 min'
    assert shown['tier']['caption'] == 'Alert interval'
    assert shown['tier']['value'] != times[0]['value']

    # After an acknowledgement the interval restarts but the total wait
    # does not: both facts stay readable and independent.
    acknowledged = _row(
        tier=TrafficTier.GREEN,
        total_active_wait_us=15 * MINUTE,
        alert_interval_us=0,
    )
    after = _rows(_context(_page([acknowledged])))[0]
    assert after['tier']['value'] == 'under 1 min'
    assert after['times'][0]['value'] == '15 min'

    # Unknown history is said, never written as zero or a made-up time.
    legacy = _row(
        state=DashboardRowState.UNKNOWN,
        tier=None,
        occupied_since=None,
        episode_opened_at=None,
        last_changed_at=None,
        total_active_wait_us=None,
        alert_interval_us=None,
        ack_unavailable_reason=AckUnavailableReason.CLOCK_UNKNOWN,
    )
    unknown = {
        t['key']: t for t in _rows(_context(_page([legacy])))[0]['times']
    }
    assert unknown['wait']['value'] == 'Not available'
    assert unknown['occupied']['value'] == 'Historical time unknown'
    assert unknown['changed']['value'] == 'Historical time unknown'
    assert 'due' not in unknown  # no episode, so nothing is "due since"
    assert all(
        unknown[key]['is_unknown'] for key in ('wait', 'occupied', 'changed')
    )
    assert unknown['created']['is_unknown'] is False

    no_episode = _row(episode_opened_at=None)
    due_line = {
        t['key']: t for t in _rows(_context(_page([no_episode])))[0]['times']
    }['due']
    assert due_line['value'] == 'Historical time unknown'
    assert due_line['is_unknown'] is True

    # Paused: the frozen total and no tier colour.
    paused = _rows(
        _context(
            _page(
                [
                    _row(
                        state=DashboardRowState.PAUSED,
                        tier=None,
                        ack_unavailable_reason=AckUnavailableReason.PAUSED,
                    )
                ]
            )
        )
    )[0]
    assert paused['times'][0]['value'] == '30 min, frozen'
    assert paused['tier']['kind'] == 'paused'
    assert paused['tier']['urgency'] is None
    assert paused['tier']['caption'] == 'Alert interval, frozen'

    # A confirmed match shows what it waited until confirmation.
    done = _rows(
        _context(
            _page(
                [
                    _row(
                        state=DashboardRowState.DONE,
                        tier=None,
                        total_active_wait_us=None,
                        alert_interval_us=None,
                        closed_episode_wait_us=14 * MINUTE,
                        ack_unavailable_reason=AckUnavailableReason.TERMINAL,
                    )
                ]
            )
        )
    )[0]
    assert done['times'][0]['value'] == '14 min (until confirmation)'


# --------------------------------------------------------- query parsing


def test_query_parser_drops_invalid_values_with_field_errors():
    hidden = generate_uuid7()
    args = {
        'scope': 'everything',
        'view': 'someday',
        'state': 'tier-purple',
        'sort': 'letzte-aktivitaet',
        'tournament': 'not-a-uuid',
        'page': '0',
    }

    query, errors = parse_dashboard_query(args, surface='admin', per_page=25)

    # Every invalid value is dropped; the scope never widens.
    assert query == DashboardQuery(per_page=25)
    assert query.scope == 'assigned'
    assert list(errors) == [
        'scope',
        'view',
        'state',
        'sort',
        'tournament',
        'page',
    ]
    assert errors['sort'] == (
        'Sort order “letzte-aktivitaet” does not exist. The list is shown'
        ' without this value.'
    )
    assert errors['tournament'] == (
        'Tournament “not-a-uuid” is not available. The list is shown'
        ' without this value.'
    )

    # Valid values pass, one invalid field does not spoil the others.
    mixed, mixed_errors = parse_dashboard_query(
        {
            'view': 'all',
            'state': 'conflict',
            'sort': 'nope',
            'page': '3',
            'scope': 'all',
            'tournament': str(hidden),
        },
        surface='admin',
        per_page=25,
    )
    assert mixed == DashboardQuery(
        scope='all',
        view='all',
        state='conflict',
        tournament_id=hidden,
        page=3,
        per_page=25,
    )
    assert list(mixed_errors) == ['sort']

    # The site ignores `scope`: no widening, and no error either.
    for scope in ('all', 'assigned', 'everything', ''):
        site_query, site_errors = parse_dashboard_query(
            {'scope': scope}, surface='site', per_page=25
        )
        assert site_query.scope == 'assigned'
        assert site_errors == {}

    # A repeated parameter is ambiguous: it is dropped, not guessed.
    twice, twice_errors = parse_dashboard_query(
        MultiDict([('sort', 'wait'), ('sort', 'urgency')]),
        surface='admin',
        per_page=25,
    )
    assert twice.sort == 'urgency'
    assert list(twice_errors) == ['sort']

    # Pages are plain ASCII digits within the bound the service accepts.
    for raw, valid in [
        ('1', True),
        ('2', True),
        ('1000000', True),
        ('000012', True),
        ('1000001', False),
        ('-1', False),
        ('+2', False),
        (' 2', False),
        ('2 ', False),
        ('1e3', False),
        ('٣', False),
        ('²', False),
        ('', False),
        ('9' * 40, False),
    ]:
        page_query, page_errors = parse_dashboard_query(
            {'page': raw}, surface='site', per_page=25
        )
        assert (page_errors == {}) is valid, raw
        assert page_query.page == (int(raw) if valid else 1), raw

    # A notice echoes the value, short and printable only.
    _, hostile = parse_dashboard_query(
        {'sort': 'x' * 100 + '\x00\u202e\n<script>'},
        surface='site',
        per_page=25,
    )
    echoed = hostile['sort'].split('“')[1].split('”')[0]
    assert echoed == 'x' * 40 + '…'
    _, controls = parse_dashboard_query(
        {'sort': 'a\x00b\u202ec\nd'}, surface='site', per_page=25
    )
    assert '“abcd”' in controls['sort']

    # The empty option of the tournament select is "no filter".
    cleared, cleared_errors = parse_dashboard_query(
        {'tournament': ''}, surface='admin', per_page=25
    )
    assert cleared.tournament_id is None
    assert cleared_errors == {}

    # R35: a tournament outside the resolved scope is dropped with a
    # notice; unknown and hidden IDs read the same.
    inside, outside = generate_uuid7(), generate_uuid7()
    for surface in ('admin', 'site'):
        kept, kept_errors = parse_dashboard_query(
            {'tournament': str(inside)},
            surface=surface,
            per_page=25,
            scope_tournament_ids=(inside, str(generate_uuid7())),
        )
        assert kept.tournament_id == inside
        assert kept_errors == {}

        dropped, dropped_errors = parse_dashboard_query(
            {'tournament': str(outside), 'view': 'all'},
            surface=surface,
            per_page=25,
            scope_tournament_ids=(inside,),
        )
        assert dropped.tournament_id is None
        assert dropped.view == 'all'  # the other filters stay
        assert list(dropped_errors) == ['tournament']
        assert str(outside) in dropped_errors['tournament']

    _, empty_scope = parse_dashboard_query(
        {'tournament': str(inside)},
        surface='admin',
        per_page=25,
        scope_tournament_ids=(),
    )
    assert list(empty_scope) == ['tournament']

    # The context shows each dropped field at its field, and one notice.
    context = _context(_page([_row()]), query, errors=errors)
    assert context['banner']['heading'] == (
        'Part of the query was invalid and was not applied.'
    )
    assert context['banner']['details'] == list(errors.values())
    filters = context['filters']
    assert (
        filters['sort']['error'] == 'This value is invalid and was not applied.'
    )
    assert filters['sort']['is_invalid'] is True
    assert filters['tournament']['error'] == 'This tournament is not available'
    assert (
        filters['state']['error']
        == 'This value is invalid and was not applied.'
    )
    assert _context(_page([_row()]))['banner'] is None

    # A tournament that the page does not offer is not shown as a filter.
    stranger = _context(_page([_row()]), _query(tournament_id=outside))
    assert stranger['query']['tournament'] is None
    assert stranger['filters']['tournament']['error'] == (
        'This tournament is not available'
    )
    assert stranger['banner'] is not None


# --------------------------------------------------------- return context


_HOSTILE_RETURNS = [
    'https://evil.example/steal',
    'http://evil.example',
    '//evil.example/x',
    '/lan-tournaments/for_party/other-party/dashboard',
    '/../../admin',
    'javascript:alert(1)',
    'data:text/html,<b>x</b>',
    '<script>alert(1)</script>',
    '"><img src=x onerror=alert(1)>',
    # Each of these carries a valid, non-default part: it must not survive.
    'view=all&evil=1',
    'view=all&party_id=other-party',
    'page=2&party=other-party',
    'view=all&view=upcoming',
    'view=all&&page=2',
    'view=all&page',
    'view=all&sort=nope',
    'page=2&state=bogus',
    'sort=wait&page=0',
    'sort=wait&tournament=https://evil.example',
    'sort=wait&page=9999999999',
    'sort=urgency%0d%0aSet-Cookie:x=1',
    'view=all%00',
    'view=all&' + 'page=2&' * 20,
    'x' * 1025,
    'view=all&' + 'sort=wait&' * 200,
]


def test_return_context_rebuilds_only_allowlisted_parameters():
    tournament = generate_uuid7()

    # No value: there is no list to go back to.
    for empty in (None, ''):
        assert (
            parse_dashboard_return(empty, surface='admin', per_page=25) is None
        )

    # What the dashboard writes, it reads back unchanged.
    queries = [
        DashboardQuery(per_page=25),
        DashboardQuery(scope='all', view='upcoming', per_page=25),
        DashboardQuery(state='tier-red', sort='wait', page=7, per_page=25),
        DashboardQuery(
            scope='all',
            view='all',
            state='ready-unavailable',
            sort='tournament',
            tournament_id=tournament,
            page=1_000_000,
            per_page=25,
        ),
    ]
    for surface in ('admin', 'site'):
        for query in queries:
            written = build_dashboard_return(query, surface=surface)
            assert 0 < len(written) <= 1024
            assert re.fullmatch(r'[A-Za-z0-9_.~%+=&-]+', written)
            expected = (
                query
                if surface == 'admin'
                else replace(query, scope='assigned')
            )
            assert (
                parse_dashboard_return(written, surface=surface, per_page=25)
                == expected
            )

    default = DashboardQuery(per_page=25)
    for hostile in _HOSTILE_RETURNS:
        for surface in ('admin', 'site'):
            query = parse_dashboard_return(
                hostile, surface=surface, per_page=25
            )
            # Anything that is not exactly an encoded list query falls
            # back to the default list, never to a partial reading.
            assert query == default, hostile

            url = build_dashboard_list_url(
                surface, 'party-of-the-destination', query
            )
            assert url.startswith('/lan-tournaments/')
            assert url == (
                '/lan-tournaments/for_party/party-of-the-destination/dashboard'
                if surface == 'admin'
                else '/lan-tournaments/orga-dashboard'
            )
            for fragment in (
                'evil',
                'script',
                'javascript',
                'other-party',
                'Set-Cookie',
                '//',
            ):
                assert fragment not in url.replace('/lan-tournaments/', '')

    # A `scope` in a site return is ignored, in an admin return it counts.
    assert parse_dashboard_return(
        'scope=all&view=all', surface='site', per_page=25
    ) == DashboardQuery(view='all', per_page=25)
    assert parse_dashboard_return(
        'scope=all&view=all', surface='admin', per_page=25
    ) == DashboardQuery(scope='all', view='all', per_page=25)

    # The admin party is the destination's, never a parameter's; the
    # anchor is the row of a match, and only a real match ID makes one.
    match_id = _match_id()
    query = DashboardQuery(view='all', sort='wait', page=3, per_page=25)
    url = build_dashboard_list_url(
        'admin', 'pixelnacht-36', query, anchor_match_id=match_id
    )
    split = urlsplit(url)
    assert split.path == '/lan-tournaments/for_party/pixelnacht-36/dashboard'
    assert parse_qs(split.query) == {
        'view': ['all'],
        'sort': ['wait'],
        'page': ['3'],
    }
    assert split.fragment == f'lt-row-{match_id}'

    for bad_anchor in ('"><x>', 'not-a-uuid', '', 42, object()):
        plain = build_dashboard_list_url(
            'site', 'ignored', query, anchor_match_id=bad_anchor
        )
        assert urlsplit(plain).fragment == ''
        assert urlsplit(plain).path == '/lan-tournaments/orga-dashboard'

    # A default list has a clean URL; a scope appears only on the admin.
    assert (
        build_dashboard_list_url('admin', 'p', DashboardQuery(per_page=25))
        == '/lan-tournaments/for_party/p/dashboard'
    )
    assert (
        build_dashboard_list_url(
            'admin', 'p', DashboardQuery(scope='all', per_page=25)
        )
        == '/lan-tournaments/for_party/p/dashboard?scope=all'
    )
    assert (
        build_dashboard_list_url(
            'site', 'p', DashboardQuery(scope='all', per_page=25)
        )
        == '/lan-tournaments/orga-dashboard'
    )

    # The back link says where it goes.
    assert (
        helpers.describe_dashboard_return(DashboardQuery(per_page=25))
        == '(Due now · Urgency · Page 1)'
    )
    assert dashboard_labels()['link_back'] == '← Back to the orga dashboard'

    # Links of a row carry the whole validated list as `return`.
    context = _context(
        _page([_row()]),
        DashboardQuery(scope='all', state='pinned', page=2, per_page=50),
    )
    row = _rows(context)[0]
    for link in (row['match_url'], row['tournament']['url']):
        returned = parse_qs(urlsplit(link).query)['return']
        assert returned == [context['return_value']]
        assert parse_dashboard_return(
            returned[0], surface='admin', per_page=50
        ) == DashboardQuery(scope='all', state='pinned', page=2, per_page=50)


# ------------------------------------------------- durations and wall time


# fmt: off
_DURATIONS = [
    (0, 'under 1 min'),
    (-5 * MINUTE, 'under 1 min'),
    (59_999_999, 'under 1 min'),
    (MINUTE, '1 min'),
    (14 * MINUTE + 59_999_999, '14 min'),
    (15 * MINUTE, '15 min'),
    (44 * MINUTE + 59_999_999, '44 min'),
    (45 * MINUTE, '45 min'),
    (59 * MINUTE + 59_999_999, '59 min'),
    (60 * MINUTE, '1 h 00 min'),
    (65 * MINUTE, '1 h 05 min'),
    (65 * MINUTE + 59_999_999, '1 h 05 min'),
    (125 * MINUTE + 1, '2 h 05 min'),
    (24 * 60 * MINUTE, '24 h 00 min'),
]
# fmt: on


def test_durations_floor_and_wall_times_follow_snapshot_day():
    # Durations are floored to whole minutes, never rounded up.
    for elapsed_us, expected in _DURATIONS:
        assert format_active_duration(elapsed_us) == expected, elapsed_us

    snapshot = datetime(2026, 10, 8, 12, 0)  # 14:00 CEST

    # The same day: the time only, whether naive (UTC) or aware.
    assert format_wall_time(datetime(2026, 10, 8, 7, 5), snapshot=snapshot) == (
        '09:05'
    )
    assert (
        format_wall_time(
            datetime(2026, 10, 8, 7, 5, tzinfo=UTC), snapshot=snapshot
        )
        == '09:05'
    )

    # Another day: a date prefix, in the viewer's language.
    other_day = datetime(2026, 10, 7, 21, 50)  # 23:50 CEST on the 7th
    assert format_wall_time(other_day, snapshot=snapshot) == 'Oct 7, 23:50'
    with force_locale('de'):
        assert format_wall_time(other_day, snapshot=snapshot) == (
            '7. Okt., 23:50'
        )
        assert helpers.format_zone_label(snapshot) == 'MESZ'
    assert helpers.format_zone_label(snapshot) == 'CEST'

    # The day is the party's day, not the UTC day: 23:50 and 00:30 CEST
    # are one UTC date apart for the snapshot below, one party day too.
    after_midnight = datetime(2026, 10, 8, 22, 30)  # 00:30 CEST, the 9th
    same_utc_day = datetime(2026, 10, 8, 21, 50)  # 23:50 CEST, the 8th
    assert format_wall_time(same_utc_day, snapshot=after_midnight) == (
        'Oct 8, 23:50'
    )
    assert format_wall_time(after_midnight, snapshot=after_midnight) == '00:30'

    # Winter time: UTC+1.
    assert (
        format_wall_time(
            datetime(2026, 1, 8, 7, 5), snapshot=datetime(2026, 1, 8, 12, 0)
        )
        == '08:05'
    )

    assert helpers.format_freshness_time(datetime(2026, 10, 8, 12, 0, 5)) == (
        '14:00:05'
    )

    # Rows write their times through the same helper.
    row = _rows(
        _context(
            _page(
                [
                    _row(
                        last_changed_at=datetime(2026, 10, 7, 21, 50),
                        occupied_since=datetime(2026, 10, 8, 7, 5),
                    )
                ]
            )
        )
    )[0]
    times = {t['key']: t['value'] for t in row['times']}
    assert times['changed'] == 'Oct 7, 23:50'
    assert times['occupied'] == '09:05'


# ------------------------------------------------------ threshold labels


def _tier_ranges(context):
    return {t['tier']: t['range'] for t in context['tiles']['items']}


def _label_minutes(label: str) -> int:
    """Return the whole minutes a duration label states."""
    if label.startswith('under'):
        return 0
    numbers = [int(n) for n in re.findall(r'\d+', label)]
    return numbers[0] * 60 + numbers[1] if 'h' in label else numbers[0]


def _contains(range_text: str, minutes: int) -> bool:
    numbers = [int(n) for n in re.findall(r'\d+', range_text)]
    if range_text.startswith('under'):
        return minutes < numbers[0]
    if range_text.startswith('from'):
        return minutes >= numbers[0]
    if len(numbers) == 1:
        return minutes == numbers[0]
    return numbers[0] <= minutes <= numbers[1]


# fmt: off
@pytest.mark.parametrize('yellow, red', [
    (15, 45), (1, 2), (59, 60), (20, 60), (5, 6), (30, 1440), (1, 1440),
])
# fmt: on
def test_threshold_labels_match_tiers_for_non_default_settings(yellow, red):
    settings = _settings(yellow, red)
    context = _context(_page(), settings=settings)
    ranges = _tier_ranges(context)

    color_of = {
        TrafficTier.GREEN: 'green',
        TrafficTier.YELLOW: 'yellow',
        TrafficTier.RED: 'red',
    }
    from byceps.services.lan_tournament.tournament_operational_domain_service import (
        derive_traffic_tier,
    )

    # Probe each side of both thresholds, to the microsecond: the tier the
    # server derives and the floored label must agree with the range text
    # of that very tier, and with no other tier's.
    probes = {0, 1, 59_999_999}
    for threshold in (yellow, red):
        for delta in (-MINUTE, -1, 0, 1, MINUTE - 1):
            probes.add(max(threshold * MINUTE + delta, 0))

    for alert_us in sorted(probes):
        tier = color_of[derive_traffic_tier(alert_us, settings)]
        minutes = alert_us // MINUTE
        assert _label_minutes(format_active_duration(alert_us)) == minutes
        containing = [
            color for color, text in ranges.items() if _contains(text, minutes)
        ]
        assert containing == [tier], (alert_us, ranges)

    # The texts themselves.
    assert ranges['green'] == f'under {yellow} min'
    assert ranges['red'] == f'from {red} min'
    assert ranges['yellow'] == (
        f'{yellow} min' if yellow == red - 1 else f'{yellow}–{red - 1} min'
    )

    # A tier is reached at the minute it names: a row of that interval
    # shows the same tier the tile range names.
    for alert_us in (yellow * MINUTE - 1, yellow * MINUTE, red * MINUTE):
        tier = derive_traffic_tier(alert_us, settings)
        shown = _rows(
            _context(
                _page([_row(tier=tier, alert_interval_us=alert_us)]),
                settings=settings,
            )
        )[0]['tier']
        assert shown['urgency'] == color_of[tier]
        assert _contains(ranges[shown['urgency']], alert_us // MINUTE)

    # The reasons name the party's yellow threshold, never a literal 15.
    for reason in (
        AckUnavailableReason.BELOW_THRESHOLD,
        AckUnavailableReason.RECENTLY_ACKNOWLEDGED,
    ):
        text = helpers.ack_unavailable_text(reason, settings)
        assert f'{yellow} min' in text
        assert (str(yellow) == '15') or ('15' not in text)


# ------------------------------------------------------------- tiles


def test_tiles_link_only_when_not_empty_and_toggle_their_tier():
    page = _page(
        [_row()],
        tier_counts=DashboardTierCounts(red=0, yellow=2, green=1),
    )
    tiles = _context(page, _query(view='all', page=3))['tiles']

    assert [t['tier'] for t in tiles['items']] == ['red', 'yellow', 'green']
    red, yellow, green = tiles['items']
    assert red['url'] is None
    assert red['aria_label'] is None
    assert red['is_zero'] is True

    assert parse_qs(urlsplit(yellow['url']).query) == {
        'view': ['all'],
        'state': ['tier-yellow'],
    }  # the page goes back to 1, the view stays
    assert yellow['aria_label'] == (
        '2 matches: Check delay (15–44 min). Filter the list to this tier'
    )
    assert green['aria_label'] == (
        '1 match: Below warning threshold (under 15 min).'
        ' Filter the list to this tier'
    )

    active = _context(page, _query(state='tier-yellow', page=2))['tiles']
    on = active['items'][1]
    assert on['is_active'] is True
    # The active tile turns its filter off again.
    assert 'state' not in parse_qs(urlsplit(on['url']).query)
    assert on['aria_label'].endswith('Remove the filter, show all matches')
    assert [t['is_active'] for t in active['items']] == [False, True, False]

    assert tiles['note'] == (
        'Green does not mean ready to play. Readiness is shown separately.'
    )


def _tile_query(url: str) -> DashboardQuery:
    query, errors = parse_dashboard_query(
        MultiDict(parse_qsl(urlsplit(url).query, keep_blank_values=True)),
        surface='admin',
        per_page=50,
    )
    assert errors == {}
    return query


# fmt: off
@pytest.mark.parametrize('color', ['red', 'yellow', 'green'])
# fmt: on
def test_a_tier_tile_on_the_upcoming_view_opens_the_due_view(color):
    page = _page(
        [_row()],
        tier_counts=DashboardTierCounts(red=3, yellow=2, green=1),
    )
    tiers = ('red', 'yellow', 'green')

    # Only due rows carry a tier: the upcoming view shows none, so a tile
    # that kept it would count matches and then list nothing.
    off = _context(page, _query(view='upcoming', page=4))['tiles']['items']
    for tier, tile in zip(tiers, off, strict=True):
        assert _tile_query(tile['url']) == _query(
            view='due', state=f'tier-{tier}'
        )

    # The active tile turns its filter off and keeps the view.
    on = _context(page, _query(view='upcoming', state=f'tier-{color}', page=2))
    items = on['tiles']['items']
    assert _tile_query(items[tiers.index(color)]['url']) == _query(
        view='upcoming'
    )
    for tier, tile in zip(tiers, items, strict=True):
        if tier != color:
            assert _tile_query(tile['url']).view == 'due'


# --------------------------------------------------------------- rows


def test_tier_cell_and_readiness_are_separate_facts():
    sides_ready = _row(
        tier=TrafficTier.RED,
        alert_interval_us=50 * MINUTE,
        ready_at_a=datetime(2026, 10, 8, 11, 3),
        ready_at_b=datetime(2026, 10, 8, 11, 5),
    )
    unready = _row(tier=TrafficTier.GREEN, alert_interval_us=3 * MINUTE)
    one = _row(ready_at_a=datetime(2026, 10, 8, 11, 3))
    lobby = _row(
        game_format=GameFormat.FREE_FOR_ALL,
        location=DashboardMatchLocation(phase=1, group_order=0, round=0,
                                        match_order=0),
        contestant_names=('Tess', 'Echo', 'Lio', 'Vale'),
        readiness_available=False,
    )
    gap = _row(readiness_available=False)
    rows = _rows(_context(_page([sides_ready, unready, one, lobby, gap])))

    ready, none, half, ffa, unavailable = rows

    # Urgency and readiness are two flags: a ready red row and an unready
    # green row show both facts, and the tier is never colour-only.
    assert (ready['tier']['kind'], ready['tier']['urgency']) == ('r', 'red')
    assert ready['tier']['name'] == 'Long delay'
    assert ready['tier']['icon'] == '■'
    assert ready['ready']['level'] == 2
    assert ready['ready']['summary'] == 'Both ready'
    assert [s['text'] for s in ready['ready']['sides']] == [
        'Kupferfüchse ready since 13:03',
        'Nachtbus ready since 13:05',
    ]

    assert (none['tier']['kind'], none['tier']['urgency']) == ('g', 'green')
    assert none['tier']['icon'] == '○'
    assert none['tier']['value'] == '3 min'
    assert none['ready']['level'] == 0
    assert none['ready']['summary'] == 'Nobody ready'
    assert [s['text'] for s in none['ready']['sides']] == [
        'Kupferfüchse not ready',
        'Nachtbus not ready',
    ]
    assert half['ready']['summary'] == 'One side ready'

    # No Ready exists for a lobby, and a gap is never "nobody ready".
    assert ffa['ready']['available'] is False
    assert ffa['ready']['unavailable_text'] == 'Not available (lobby format)'
    assert ffa['ready']['level'] is None
    assert unavailable['ready']['unavailable_text'] == 'Not available'
    assert unavailable['ready']['summary'] is None

    # A lobby is named and listed; a pairing has two sides.
    assert ffa['matchup']['kind'] == 'lobby'
    assert ffa['matchup']['title'] == 'Lobby 1 · 4 participants'
    assert ffa['matchup']['participants_label'] == 'Participants in Lobby 1'
    assert ffa['matchup']['participants'] == ['Tess', 'Echo', 'Lio', 'Vale']
    assert ffa['format_label'] == 'Free for all'
    assert ready['matchup']['title'] == 'Kupferfüchse against Nachtbus'
    assert ready['format_label'] == 'Knockout'

    # The state cells carry no tier and no readiness colour.
    cells = {}
    for state in DashboardRowState:
        if state is DashboardRowState.DUE:
            continue
        shown = _rows(_context(_page([_row(state=state, tier=None)])))[0]
        cells[state] = shown['tier']
        assert shown['tier']['urgency'] is None
    assert {s: c['kind'] for s, c in cells.items()} == {
        DashboardRowState.PAUSED: 'paused',
        DashboardRowState.UPCOMING: 'upcoming',
        DashboardRowState.PARTIAL: 'partial',
        DashboardRowState.BYE: 'bye',
        DashboardRowState.AWAITING_LOBBY: 'wait',
        DashboardRowState.DONE: 'done',
        DashboardRowState.UNKNOWN: 'unknown',
    }
    assert [c['name'] for c in cells.values()] == [
        'Paused', 'Upcoming', 'Incomplete', 'Bye', 'Waiting for lobby',
        'Completed', 'Time unknown',
    ]

    # Rows without a second contestant say why.
    partial = _rows(
        _context(
            _page([_row(state=DashboardRowState.PARTIAL, tier=None,
                        contestant_names=('Nachtbus',))])
        )
    )[0]
    assert partial['matchup']['side_b']['text'] == 'Opponent still open'
    assert partial['ready'] is None
    bye = _rows(
        _context(
            _page([_row(state=DashboardRowState.BYE, tier=None,
                        contestant_names=('Funkloch',))])
        )
    )[0]
    assert bye['matchup']['side_b']['text'] == 'no opponent (bye)'
    assert bye['matchup']['side_b']['is_open'] is True


def test_signals_and_notes_use_the_rows_own_facts():
    row = _row(
        pinned_at=datetime(2026, 10, 8, 11, 10),
        pinned_by_name='Alex',
        pin_revision=3,
        has_prior_episode=True,
        episode_opened_at=datetime(2026, 10, 8, 13, 32),
        review_available=True,
        review_open=True,
        conflicts=(_conflict('Nori', refs=(_ref(),)),),
    )
    shown = _rows(_context(_page([row])))[0]

    assert [(s['kind'], s['icon'], s['text']) for s in shown['signals']] == [
        ('pin', '◆', 'Pinned by Alex at 13:10'),
        ('conflict', '⇄', 'Conflict'),
        ('review', '⚑', 'Review open'),
        ('episode', '↻', 'New episode since 15:32'),
    ]

    # An absent review feature is "not available", never "no review".
    plain = _rows(_context(_page([_row()])))[0]
    assert [(s['kind'], s['text']) for s in plain['signals']] == [
        ('review_unavailable', 'Review: not available')
    ]

    # An unnamed pinner reads like a deleted orga.
    nameless = _rows(
        _context(
            _page([_row(pinned_at=datetime(2026, 10, 8, 11, 10),
                        pinned_by_name=None)])
        )
    )[0]
    assert nameless['signals'][0]['text'] == (
        'Pinned by Deleted orga at 13:10'
    )

    def note(**fields):
        return _rows(_context(_page([_row(**fields)])))[0]['note']

    def status(code, **params):
        return DashboardStatusNote(code=code, params=tuple(params.items()))

    group = DashboardMatchLocation(phase=1, group_order=0, round=2,
                                   match_order=1)
    # Notes count from one: stored rounds are zero-based.
    assert note(
        state=DashboardRowState.UPCOMING, tier=None, location=group,
        status_note=status('earlier_round_open', round=1),
    ) == 'Round 2 of Group A is still open. Future match, so no conflict.'
    assert note(
        state=DashboardRowState.UPCOMING, tier=None,
        location=DashboardMatchLocation(phase=1, round=3, match_order=0),
        status_note=status('earlier_round_open', round=0),
    ) == 'Round 1 is still open. Future match, so no conflict.'
    assert note(
        state=DashboardRowState.BYE, tier=None, contestant_names=('Funkloch',),
        status_note=status('bye_advance'),
    ) == 'Funkloch advances without a match.'
    lobby = dict(
        state=DashboardRowState.AWAITING_LOBBY, tier=None,
        game_format=GameFormat.FREE_FOR_ALL,
    )
    assert note(**lobby, status_note=status(
        'lobby_waiting', filled=2, size=4, after_round=1)) == (
        '2 of 4 places filled. The lobby is only created completely after'
        ' round 2.'
    )
    # A second-round lobby (stored round 1) waits for round 1.
    assert note(**lobby, status_note=status(
        'lobby_waiting', filled=3, size=4, after_round=0)) == (
        '3 of 4 places filled. The lobby is only created completely after'
        ' round 1.'
    )
    assert note(**lobby, status_note=status('lobby_waiting', filled=1)) == (
        '1 place filled.'
    )
    assert note(**lobby, status_note=status('lobby_waiting', filled=3)) == (
        '3 places filled.'
    )
    done = dict(state=DashboardRowState.DONE, tier=None)
    assert note(
        **done, last_changed_at=datetime(2026, 10, 8, 11, 34),
        status_note=status('result_confirmed', score_a=2, score_b=1),
    ) == 'Result 2:1 confirmed at 13:34.'
    assert note(
        **done, last_changed_at=None,
        status_note=status('result_confirmed', score_a=2, score_b=1),
    ) == 'Result 2:1 confirmed.'
    assert note(
        episode_opened_at=datetime(2026, 10, 8, 13, 32),
        status_note=status('corrected_reopened'),
    ) == (
        'The result was corrected and reopened at 15:32. Earlier checks'
        ' belong to the previous episode and do not apply here.'
    )

    # Unknown or malformed notes say nothing, never fail.
    assert note(status_note=status('something_new', round=1)) is None
    assert note(status_note=status('earlier_round_open', round='x')) is None
    assert note(status_note=status('bye_advance'), contestant_names=()) is None
    assert note(status_note=None) is None


def _ack_summary(actor, minute, comment=None, revision=1):
    return DashboardAcknowledgementSummary(
        id=generate_uuid7(),
        revision=revision,
        actor_display_name=actor,
        occurred_at=datetime(2026, 10, 8, 12, minute),
        comment=comment,
    )


def test_ack_area_offers_the_action_or_names_the_reason():
    settings = _settings(15, 45)

    offered = _row(
        tier=TrafficTier.YELLOW,
        alert_interval_us=16 * MINUTE,
        ack_unavailable_reason=None,
        ack_revision=4,
    )
    shown = _rows(_context(_page([offered]), settings=settings))[0]['ack']
    assert shown['offered'] is True
    assert shown['opener_label'] == 'Log a check…'
    assert shown['unavailable_text'] is None
    form = shown['form']
    assert form['episode'] == str(offered.episode_id)
    assert form['revision'] == 4
    assert form['csrf_token'] == 'token-1'
    assert form['heading'] == 'Delay checked – record it'
    assert form['counter_text'] == '0 / 500 characters · text only'
    assert form['max_length'] == 500
    assert urlsplit(form['action_url']).path == (
        f'/lan-tournaments/for_party/{PARTY}/dashboard/matches/'
        f'{offered.match_id}/ack'
    )
    assert shown['record'] is None
    assert shown['history'] is None

    # Re-escalation: the record stays, the follow-up is offered beside it.
    latest = _ack_summary('Mara', 15, 'Headset ist da', revision=2)
    older = _ack_summary('Jonas', 14, None, revision=1)
    again = _row(
        tier=TrafficTier.RED,
        alert_interval_us=46 * MINUTE,
        ack_unavailable_reason=None,
        latest_acknowledgement=latest,
        recent_acknowledgements=(latest, older),
        acknowledgement_count=5,
    )
    shown = _rows(_context(_page([again]), settings=settings))[0]['ack']
    assert shown['opener_label'] == 'Check again'
    assert shown['form']['heading'] == 'Delay checked again – record it'
    assert shown['record']['text'] == 'Last checked by Mara at 14:15'
    assert shown['record']['comment'] == 'Headset ist da'
    assert shown['record']['follow'] is True
    assert shown['record']['follow_tier'] == 'red'
    assert shown['record']['follow_text'] == 'Delay again – check once more'
    history = shown['history']
    assert history['count_text'] == '(5 in this episode)'
    assert [(i['actor'], i['time'], i['is_current'], i['comment'])
            for i in history['items']] == [
        ('Mara', '14:15', True, 'Headset ist da'),
        ('Jonas', '14:14', False, None),
    ]
    assert history['items'][0]['current_label'] == 'current'
    assert history['items'][1]['no_comment_label'] == 'no comment'

    # Just acknowledged, back to green: the record stays, no follow-up.
    quiet = _row(
        tier=TrafficTier.GREEN,
        alert_interval_us=2 * MINUTE,
        ack_unavailable_reason=AckUnavailableReason.RECENTLY_ACKNOWLEDGED,
        latest_acknowledgement=latest,
        recent_acknowledgements=(latest,),
        acknowledgement_count=1,
    )
    shown = _rows(_context(_page([quiet]), settings=settings))[0]['ack']
    assert shown['offered'] is False
    assert shown['form'] is None
    assert shown['opener_label'] is None
    assert shown['record']['follow'] is False
    assert shown['history'] is None
    assert shown['unavailable_text'] == (
        'Just checked. Again from 15 min of alert interval.'
    )

    # Every reason has its words, and none reads a literal threshold.
    texts = {
        reason: helpers.ack_unavailable_text(reason, _settings(7, 9))
        for reason in AckUnavailableReason
    }
    assert texts == {
        AckUnavailableReason.BELOW_THRESHOLD:
            'Checks start at 7 min of alert interval.',
        AckUnavailableReason.RECENTLY_ACKNOWLEDGED:
            'Just checked. Again from 7 min of alert interval.',
        AckUnavailableReason.PAUSED: 'Paused – checking not possible.',
        AckUnavailableReason.NOT_DUE: 'Not due – no checking.',
        AckUnavailableReason.CLOCK_UNKNOWN: 'No time base, no checking.',
        AckUnavailableReason.TERMINAL: 'Completed – no actions.',
    }

    # An offer without an episode cannot be acted on: it is not offered.
    broken = _row(
        tier=TrafficTier.YELLOW,
        alert_interval_us=20 * MINUTE,
        ack_unavailable_reason=None,
        episode_id=None,
    )
    shown = _rows(_context(_page([broken])))[0]['ack']
    assert shown['offered'] is False
    assert shown['form'] is None
    assert shown['unavailable_reason'] == 'clock_unknown'

    assert helpers.format_comment_counter(37) == (
        '37 / 500 characters · text only'
    )


def test_pin_form_is_native_and_read_only_on_terminal_rows():
    free = _row(pin_revision=2)
    pinned = _row(
        pinned_at=datetime(2026, 10, 8, 11, 10),
        pinned_by_name='Alex',
        pin_revision=3,
    )
    terminal = _row(
        state=DashboardRowState.DONE,
        tier=None,
        pinned_at=datetime(2026, 10, 8, 11, 10),
        pinned_by_name='Alex',
        pin_revision=1,
    )
    bye = _row(state=DashboardRowState.BYE, tier=None)
    paused = _row(state=DashboardRowState.PAUSED, tier=None)

    shown = [
        r['pin']
        for r in _rows(
            _context(_page([free, pinned, terminal, bye, paused]))
        )
    ]
    assert [p['offered'] for p in shown] == [True, True, False, False, True]
    assert [p['read_only'] for p in shown] == [False, False, True, True, False]

    # The form posts the state it asks for, at the revision it saw.
    assert (shown[0]['target'], shown[0]['label'], shown[0]['revision']) == (
        'true', 'Pin for the orga team', 2
    )
    assert (shown[1]['target'], shown[1]['label'], shown[1]['revision']) == (
        'false', 'Remove pin', 3
    )
    assert shown[0]['csrf_token'] == 'token-1'
    assert shown[0]['return_value'] == _context(_page())['return_value']
    assert urlsplit(shown[0]['action_url']).path.endswith(
        f'/dashboard/matches/{free.match_id}/pin'
    )
    # A terminal row keeps its tag but offers nothing.
    assert shown[2]['action_url'] is None
    assert shown[2]['is_pinned'] is True


# ------------------------------------------- filters, count, empty, pager


def test_filters_chips_and_views_keep_the_query():
    choices = (
        DashboardTournamentRef(tournament_id=_tournament_id(), name='Neon-Duell'),
        DashboardTournamentRef(tournament_id=_tournament_id(), name='Kupfer-Cup'),
    )
    page = _page([_row()], tournament_choices=choices)
    query = _query(
        scope='all', view='upcoming', state='ready-both', sort='wait',
        tournament_id=choices[0].tournament_id, page=4,
    )
    context = _context(page, query)

    filters = context['filters']
    assert [o['label'] for o in filters['tournament']['options']] == [
        'All tournaments of this party', 'Neon-Duell', 'Kupfer-Cup',
    ]
    assert [o['selected'] for o in filters['tournament']['options']] == [
        False, True, False,
    ]
    assert [o['label'] for o in filters['state']['options']] == [
        'All states', 'Tier: Long delay', 'Tier: Check delay',
        'Tier: Below warning threshold', 'Nobody ready', 'One side ready',
        'Both ready', 'Readiness not available', 'With conflict',
        'Review open', 'Pinned',
    ]
    assert [o['value'] for o in filters['state']['options']
            if o['selected']] == ['ready-both']
    assert [o['label'] for o in filters['sort']['options']] == [
        'Urgency', 'Total active wait', 'Tournament',
    ]
    assert filters['applied'] == [
        'Upcoming matches', 'Neon-Duell', 'Both ready', 'Sort: Total active wait',
    ]
    assert filters['has_applied'] is True
    assert context['is_default_query'] is False
    # Reset goes to the default list of the same scope.
    assert urlsplit(filters['reset_url']).query == 'scope=all'
    assert filters['hidden'] == [['scope', 'all'], ['view', 'upcoming']]

    views = {v['key']: v for v in context['views']}
    assert [v['label'] for v in context['views']] == [
        'Due now', 'Upcoming matches', 'All matches',
    ]
    assert [v['current'] for v in context['views']] == [False, True, False]
    # A view link keeps every other parameter and resets the page.
    query_of_all = parse_qs(urlsplit(views['all']['url']).query)
    assert query_of_all['view'] == ['all']
    assert query_of_all['state'] == ['ready-both']
    assert query_of_all['sort'] == ['wait']
    assert 'page' not in query_of_all

    scope = context['scope']
    assert scope['is_fixed'] is False
    assert [o['checked'] for o in scope['options']] == [False, True]
    assert ['view', 'upcoming'] in scope['hidden']
    assert all(name != 'scope' and name != 'page'
               for name, _ in scope['hidden'])

    default = _context(page)
    assert default['filters']['applied'] == []
    assert default['filters']['reset_url'] is None
    assert default['is_default_query'] is True
    assert default['filters']['tournament']['options'][0]['label'] == (
        'All assigned tournaments'
    )

    site = _context(page, surface='site')
    assert site['scope']['is_fixed'] is True
    assert site['scope']['options'] == []
    assert site['scope']['label'] == 'Assigned tournaments'
    assert site['poll']['url'] == '/lan-tournaments/orga-dashboard/poll'
    assert site['poll']['seconds'] == 30
    assert site['filters']['hidden'] == [['view', 'due']]


def test_count_and_empty_states_are_distinct():
    # A populated list.
    rows = [_row(), _row()]
    populated = _context(
        _page(rows, total_count=120, total_pages=3, page=2), _query(page=2)
    )
    assert populated['count']['heading'] == '120 matches'
    assert populated['count']['context'] == (
        'Due now · Assigned tournaments · Page 2 of 3'
    )
    assert populated['empty'] is None
    assert _context(_page(rows[:1], total_count=1))['count']['heading'] == (
        '1 match'
    )

    def empty(reason, query=None, **fields):
        return _context(
            _page(total_count=0, empty_reason=reason, **fields), query
        )['empty']

    # S6c: no assigned tournaments. The admin may widen, a site could not.
    no_assignment = empty('no_assignment')
    assert no_assignment['kind'] == 'no_assignment'
    assert no_assignment['bare'] is True
    assert no_assignment['heading'] == (
        'You have no tournaments assigned at this party.'
    )
    assert no_assignment['actions'][0]['label'] == (
        'Show all tournaments of this party'
    )
    assert urlsplit(no_assignment['actions'][0]['url']).query == 'scope=all'
    site_card = _context(
        _page(total_count=0, empty_reason='no_assignment'), surface='site'
    )['empty']
    assert site_card['body'] == (
        'To be assigned a tournament, the tournament team has to enter you'
        ' as orga.'
    )
    assert site_card['actions'][0]['url'] == '/lan-tournaments/'

    # S6d: nothing is due.
    no_demand = empty('no_current_demand')
    assert no_demand['heading'] == 'No match is due right now.'
    assert [a['label'] for a in no_demand['actions']] == [
        'Show upcoming matches', 'All matches',
    ]
    assert [parse_qs(urlsplit(a['url']).query)['view']
            for a in no_demand['actions']] == [['upcoming'], ['all']]

    # S6e: fixtures that have no demand, with the breakdown.
    breakdown = empty(
        'no_actionable_fixtures',
        non_actionable_counts=DashboardNonActionableCounts(
            paused=2, pre_start=1, partial=1
        ),
    )
    assert breakdown['heading'] == 'No match is due at the moment.'
    assert breakdown['body'] == (
        '4 matches in your tournaments have no current demand: 2 paused,'
        ' 1 before the tournament start, 1 incomplete.'
    )
    single = empty(
        'no_actionable_fixtures',
        non_actionable_counts=DashboardNonActionableCounts(paused=1),
    )
    assert single['body'].startswith(
        '1 match in your tournaments has no current demand: 1 paused,'
    )
    assert breakdown['actions'][0]['label'] == 'Show all matches'

    # S6f: filters that fit nothing stay editable and can be reset.
    choices = (
        DashboardTournamentRef(tournament_id=_tournament_id(), name='Neon-Duell'),
    )
    tournament_id = choices[0].tournament_id
    no_match = empty(
        'no_filter_matches',
        _query(state='conflict', tournament_id=tournament_id, view='upcoming',
               scope='all'),
        tournament_choices=choices,
    )
    assert no_match['heading'] == 'No match fits these filters.'
    assert no_match['context'] == 'Neon-Duell · With conflict · Upcoming matches'
    assert no_match['actions'][0]['label'] == 'Reset filters'
    assert urlsplit(no_match['actions'][0]['url']).query == 'scope=all'

    # S6h: a page that has gone away. It stays, it is not clamped.
    gone = _context(
        _page(total_count=5, total_pages=1, page=2, per_page=50),
        _query(page=2),
    )
    assert gone['beyond_last_page'] is True
    assert gone['count']['heading'] == '5 matches'
    assert gone['count']['context'] == (
        'Due now · Assigned tournaments · Page 2 no longer exists'
    )
    card = gone['empty']
    assert card['kind'] == 'page_empty'
    assert card['heading'] == 'Page 2 is empty now.'
    assert card['body'] == (
        'Matches have been confirmed since your last refresh. There is only'
        ' 1 page left.'
    )
    assert card['actions'][0]['label'] == 'To page 1'
    assert 'page' not in parse_qs(urlsplit(card['actions'][0]['url']).query)

    # Leaderboard-only tournaments are note cards after the list.
    leaderboard = DashboardTournamentRef(
        tournament_id=_tournament_id(), name='Highscore-Cup', game='Pong'
    )
    cards = _context(
        _page([_row()], leaderboard_only_tournaments=(leaderboard,)),
        _query(view='all'),
    )['cards']
    assert [c['title'] for c in cards] == ['Highscore-Cup · Pong']
    assert cards[0]['link_label'] == 'Open leaderboard'
    assert str(leaderboard.tournament_id) in cards[0]['url']


def test_pager_is_bounded_and_keeps_the_query():
    def pager(total, current, **query):
        return _context(
            _page(
                [_row()],
                total_pages=total,
                page=current,
                total_count=total * 50,
            ),
            _query(page=current, **query),
        )['pager']

    assert _context(_page([_row()]))['pager'] is None

    two = pager(2, 1, sort='wait')
    assert two['previous']['url'] is None
    assert two['next']['url'] is not None
    assert [(i['number'], i['current']) for i in two['items']] == [
        (1, True), (2, False),
    ]
    assert parse_qs(urlsplit(two['next']['url']).query) == {
        'sort': ['wait'], 'page': ['2'],
    }
    assert two['aria_label'] == 'Pages'

    # A long list shows a window around the page, with gaps.
    middle = pager(40, 20)
    numbers = [i.get('number') for i in middle['items']]
    assert numbers == [1, None, 18, 19, 20, 21, 22, None, 40]
    assert [i['gap'] for i in middle['items']].count(True) == 2
    assert middle['previous']['url'] is not None
    assert middle['next']['url'] is not None
    # Past the last page, "previous" leads back to the last page.
    beyond = _context(
        _page(total_count=100, total_pages=2, page=5), _query(page=5)
    )['pager']
    assert parse_qs(urlsplit(beyond['previous']['url']).query) == {
        'page': ['2']
    }
    assert beyond['next']['url'] is None
    assert [i.get('number') for i in beyond['items']] == [1, 2]
    assert [i['current'] for i in beyond['items']] == [False, False]

    last = pager(40, 40)
    assert last['next']['url'] is None
    assert [i.get('number') for i in last['items']] == [1, None, 38, 39, 40]


# -------------------------------------------------------- context shape


def test_admin_routes_need_the_party_and_the_site_ignores_it():
    with pytest.raises(ValueError):
        build_dashboard_context(
            _page(), _query(), _settings(), surface='admin', csrf_token='t'
        )

    context = build_dashboard_context(
        _page([_row()]),
        _query(),
        _settings(poll=45),
        surface='site',
        csrf_token='t',
        party_id='ignored-on-the-site',
    )
    assert 'ignored-on-the-site' not in json.dumps(context)
    assert context['poll']['seconds'] == 45
    row = _rows(context)[0]
    assert urlsplit(row['match_url']).path.startswith(
        '/lan-tournaments/matches/'
    )
    assert urlsplit(row['pin']['action_url']).path == (
        f'/lan-tournaments/orga-dashboard/matches/{row["match_id"]}/pin'
    )
    assert row['id'] == f'lt-row-{row["match_id"]}'

    freshness = context['freshness']
    assert freshness['time'] == '14:00:00'
    assert freshness['zone'] == 'CEST'
    assert freshness['as_of'] == '2026-10-08T12:00:00.000000Z'
    assert freshness['status_text'] == 'Refreshes automatically every 45 s'


def test_every_state_and_surface_renders_plain_data():
    for surface in ('admin', 'site'):
        for state in DashboardRowState:
            row = _row(state=state, tier=None, episode_id=None)
            _walk(_context(_page([row]), surface=surface))
            json.dumps(_context(_page([row]), surface=surface))


def test_row_context_has_the_same_keys_for_every_state():
    # A strict template reads a key it did not get as an error: every part
    # of a row has one shape, whatever the state of the row.
    latest = _ack_summary('Mara', 15)
    variants = [
        _row(state=state, tier=None, episode_id=None)
        for state in DashboardRowState
    ] + [
        _row(),
        _row(
            game_format=GameFormat.FREE_FOR_ALL,
            contestant_names=('Tess', 'Echo'),
        ),
        _row(
            tier=TrafficTier.RED,
            alert_interval_us=50 * MINUTE,
            ack_unavailable_reason=None,
            latest_acknowledgement=latest,
            recent_acknowledgements=(latest, _ack_summary('Jonas', 14)),
            conflicts=(_conflict('Nori', refs=(_ref(),), external=True),),
            pinned_at=datetime(2026, 10, 8, 11, 10),
            pinned_by_name='Alex',
            readiness_available=False,
            review_available=True,
            review_open=True,
        ),
    ]
    shown = _rows(_context(_page(variants)))

    def shape(row):
        return {
            'row': set(row),
            'tournament': set(row['tournament']),
            'matchup': set(row['matchup']),
            'tier': set(row['tier']),
            'orgas': set(row['orgas']),
            'ack': set(row['ack']),
            'pin': set(row['pin']),
            'times': {frozenset(t) for t in row['times']},
        }

    shapes = [shape(row) for row in shown]
    assert all(one == shapes[0] for one in shapes[1:])

    assert {frozenset(sig) for row in shown for sig in row['signals']} == {
        frozenset(['kind', 'icon', 'text'])
    }

    # The sub-parts that are filled only sometimes have a fixed shape too.
    ready_shapes = {
        frozenset(row['ready']) for row in shown if row['ready'] is not None
    }
    assert len(ready_shapes) == 1
    form_shapes = {
        frozenset(row['ack']['form'])
        for row in shown
        if row['ack']['form'] is not None
    }
    assert len(form_shapes) == 1
    record_shapes = {
        frozenset(row['ack']['record'])
        for row in shown
        if row['ack']['record'] is not None
    }
    assert len(record_shapes) == 1
    assert {frozenset(b) for row in shown for b in row['conflicts']} == {
        frozenset(['kind', 'name', 'role', 'tail', 'text', 'refs'])
    }


def test_labels_are_final_text_or_raw_templates():
    labels = dashboard_labels()
    assert all(type(value) is str and value for value in labels.values())

    for key, value in labels.items():
        assert ('%(' in value) is key.endswith('_template'), key

    # A template keeps its placeholders for a client to fill.
    assert labels['ack_success_template'] % {'time': '14:15:04', 'wait': '15 min'} == (
        'Check recorded (server time 14:15:04). The alert interval restarts;'
        ' total wait unchanged at 15 min.'
    )
    assert labels['csrf_invalid'] == (
        'The form check has expired. Please reload the page;'
        ' your draft stays visible.'
    )
    assert labels['ack_stale'] == 'dashboard_ack_conflict'


# ----------------------------------------------------------- transport


def test_fragment_and_error_bodies_are_the_shared_transport_shape():
    body = serialize_dashboard_fragment(
        '<section></section>',
        as_of=datetime(2026, 10, 8, 12, 0, 5, 120),
        poll_seconds=30,
    )
    assert body == {
        'html': '<section></section>',
        'as_of': '2026-10-08T12:00:05.000120Z',
        'poll_seconds': 30,
    }
    # Fixed width: snapshot times compare as strings.
    earlier = serialize_dashboard_fragment(
        '', as_of=datetime(2026, 10, 8, 12, 0, 5), poll_seconds=30
    )
    assert len(earlier['as_of']) == len(body['as_of'])
    assert earlier['as_of'] < body['as_of']


    aware = serialize_dashboard_fragment(
        '', as_of=datetime(2026, 10, 8, 14, 0, 5, tzinfo=UTC), poll_seconds=5
    )
    assert aware['as_of'] == '2026-10-08T14:00:05.000000Z'

    fragment = {'html': '<p></p>', 'as_of': 'x', 'poll_seconds': 30}
    assert serialize_dashboard_success(
        committed_at=datetime(2026, 10, 8, 12, 15, 4), fragment=fragment
    ) == {'committed_at': '2026-10-08T12:15:04.000000Z', 'fragment': fragment}
    expected = {
        dashboard_service.DASHBOARD_UNAUTHENTICATED_ERROR:
            ('session_expired', 401),
        dashboard_service.DASHBOARD_FORBIDDEN_ERROR: ('access_revoked', 403),
        'csrf_invalid': ('csrf_invalid', 403),
        coordination.DASHBOARD_MATCH_NOT_FOUND_ERROR: ('unavailable', 404),
        coordination.DASHBOARD_PIN_CONFLICT_ERROR: ('stale', 409),
        coordination.DASHBOARD_ACK_CONFLICT_ERROR: ('stale', 409),
        coordination.DASHBOARD_MATCH_TERMINAL_ERROR: ('refused', 409),
        coordination.DASHBOARD_ACK_PAUSED_ERROR: ('refused', 409),
        coordination.DASHBOARD_ACK_COMMENT_INVALID_ERROR: ('invalid', 422),
        dashboard_service.DASHBOARD_QUERY_INVALID_ERROR: ('invalid', 422),
    }
    for error, (code, status) in expected.items():
        error_body, error_status = serialize_dashboard_error(error)
        assert (error_body['error'], error_status) == (code, status)
        assert set(error_body) == {'error', 'message'}

    # A CSRF failure reads as the form check, never as lost access.
    body, status = serialize_dashboard_error('csrf_invalid')
    assert status == 403
    assert body['error'] == 'csrf_invalid'
    assert body['error'] != 'access_revoked'
    assert body['message'] == dashboard_labels()['csrf_invalid']

    # A stale or refused answer carries the whole panel and the draft flag.
    body, status = serialize_dashboard_error(
        coordination.DASHBOARD_ACK_CONFLICT_ERROR,
        fragment=fragment,
        draft_target=True,
    )
    assert body['fragment'] == fragment
    assert body['draft_target'] is True
    assert body['message'] == 'dashboard_ack_conflict'  # no catalogue here

    with pytest.raises(ValueError):
        serialize_dashboard_error('something_unknown')


def test_destination_endpoints_are_the_routes_of_the_real_blueprints():
    from byceps.services.lan_tournament.blueprints.admin import (
        views as admin_views,
    )
    from byceps.services.lan_tournament.blueprints.site import (
        views as site_views,
    )

    for surface, views in (('admin', admin_views), ('site', site_views)):
        endpoints = helpers.DASHBOARD_ENDPOINTS[surface]
        blueprint_name, view_match = endpoints['match'].split('.')
        assert views.blueprint.name == blueprint_name
        assert callable(getattr(views, view_match))
        assert endpoints['tournament'] == f'{blueprint_name}.view'
        assert callable(views.view)
        assert all(
            endpoint.startswith(f'{blueprint_name}.')
            for endpoint in endpoints.values()
        )

    assert callable(site_views.index)  # the target of the site's S6c card


def test_every_dashboard_error_code_of_the_services_is_mapped():
    codes = set()
    for module in (dashboard_service, coordination):
        for name, value in vars(module).items():
            if (
                name.startswith('DASHBOARD_')
                and name.endswith('_ERROR')
                and isinstance(value, str)
            ):
                codes.add(value)

    assert codes
    assert codes - set(helpers.TRANSPORT_ERRORS) == set()
    # Only the stable codes of the contract exist.
    assert {code for code, _ in helpers.TRANSPORT_ERRORS.values()} == {
        'session_expired',
        'access_revoked',
        'csrf_invalid',
        'unavailable',
        'stale',
        'refused',
        'invalid',
    }


# -------------------------------------------------------- German copy


@functools.cache
def _catalog():
    with PO.open('rb') as f:
        return read_po(f, locale='de')


def _german(msgid):
    message = _catalog().get(msgid)
    if message is None or message.fuzzy:
        return None
    string = message.string
    return string if isinstance(string, str) else None


def _plural_german(msgid):
    message = _catalog().get(msgid)
    if message is None or message.fuzzy:
        return None
    return message.string


def _placeholders(text):
    return set(_PLACEHOLDER.findall(text.replace('%%', '')))


@functools.cache
def _module_msgids():
    singular, plural = set(), set()
    for _, message, _, _ in extract_from_file(
        'python', MODULE, keywords={'gettext': None, 'ngettext': (1, 2)}
    ):
        if isinstance(message, tuple):
            plural.add(message[:2])
        else:
            singular.add(message)
    return singular, plural


def test_extraction_finds_the_vocabulary_of_the_module():
    singular, plural = _module_msgids()
    assert {
        'Orga dashboard',
        'Log a check…',
        'Check again',
        'Record the check',
        'Deleted orga',
        'dashboard_ack_conflict',
        'Comment (optional)',
    } <= singular
    assert ('%(count)d match', '%(count)d matches') in plural
    assert len(singular) > 150


def test_every_msgid_of_the_module_has_german_with_the_same_placeholders():
    singular, plural = _module_msgids()

    missing, wrong = [], []
    for msgid in sorted(singular):
        german = _german(msgid)
        if german is None:
            missing.append(msgid)
        elif _placeholders(german) != _placeholders(msgid):
            wrong.append(msgid)

    for one, many in sorted(plural):
        forms = _plural_german(one)
        if not forms or len(forms) != 2 or not all(forms):
            missing.append(one)
            continue
        for form, source in zip(forms, (one, many), strict=True):
            if _placeholders(form) != _placeholders(source):
                wrong.append(source)

    assert missing == []
    assert wrong == []


# fmt: off
@pytest.mark.parametrize('msgid, german', [
    # D3: the opener, the follow-up, the submit; the tier keeps its name.
    ('Log a check…', 'Prüfung erfassen…'),
    ('Check again', 'Nochmals prüfen'),
    ('Record the check', 'Prüfung festhalten'),
    ('Check delay', 'Verzögerung prüfen'),
    # R11: the reasons, with the party's threshold.
    ('Checks start at %(minutes)d min of alert interval.',
     'Prüfen ab %(minutes)d min Alarmintervall.'),
    ('Just checked. Again from %(minutes)d min of alert interval.',
     'Gerade geprüft. Erneut ab %(minutes)d min Alarmintervall.'),
    ('Paused – checking not possible.', 'Pausiert – Prüfen nicht möglich.'),
    ('Not due – no checking.', 'Nicht fällig – kein Prüfen.'),
    ('No time base, no checking.', 'Ohne Zeitbasis kein Prüfen.'),
    ('Completed – no actions.', 'Abgeschlossen – keine Aktionen.'),
    # R22: the back link.
    ('← Back to the orga dashboard', '← Zurück zum Orga-Dashboard'),
    ('Back to the orga dashboard', 'Zurück zum Orga-Dashboard'),
    # Tiers, thresholds and the legend.
    ('Long delay', 'Lange Verzögerung'),
    ('Below warning threshold', 'Unter Warnschwelle'),
    ('from %(minutes)d min', 'ab %(minutes)d min'),
    ('%(low)d–%(high)d min', '%(low)d–%(high)d min'),
    ('under %(minutes)d min', 'unter %(minutes)d min'),
    ('Green does not mean ready to play. Readiness is shown separately.',
     'Grün bedeutet nicht spielbereit. Bereitschaft steht separat.'),
    # Durations, times.
    ('under 1 min', 'unter 1 min'),
    ('%(hours)d h %(minutes)02d min', '%(hours)d h %(minutes)02d min'),
    ('Total active wait', 'Aktive Wartezeit gesamt'),
    ('Due since (this episode)', 'Fällig seit (diese Episode)'),
    ('Last match change', 'Letzte Match-Änderung'),
    ('%(time)s · age %(age)s', '%(time)s · Alter %(age)s'),
    # Conflicts.
    ('is needed in another match at the same time:',
     'ist gleichzeitig in einer anderen Begegnung benötigt:'),
    ('is needed in a match outside your tournaments at the same time.',
     ('wird gleichzeitig in einer Begegnung außerhalb deiner Turniere'
     ' benötigt.')),
    ('(member of %(team)s)', '(Mitglied von %(team)s)'),
    ('as member of %(team)s', 'als Mitglied von %(team)s'),
    ('In this list: page %(page)d', 'In dieser Liste: Seite %(page)d'),
    # Freshness.
    ('Refresh failed – the data shown is out of date.',
     'Aktualisierung fehlgeschlagen – angezeigte Daten sind veraltet.'),
    ('Automatic refresh paused while a form is being edited',
     ('Automatische Aktualisierung angehalten, solange ein Formular'
     ' bearbeitet wird')),
    # The invalid query.
    ('Part of the query was invalid and was not applied.',
     'Ein Teil der Abfrage war ungültig und wurde nicht angewendet.'),
    ('This value is invalid and was not applied.',
     'Dieser Wert ist ungültig und wurde nicht angewendet.'),
    ('This tournament is not available', 'Dieses Turnier ist nicht verfügbar'),
    (('%(field)s “%(value)s” does not exist. The list is shown without this'
     ' value.'),
     ('%(field)s „%(value)s“ gibt es nicht. Angezeigt wird die Liste ohne'
     ' diesen Wert.')),
    # Empty states.
    ('You have no tournaments assigned at this party.',
     'Dir sind auf dieser Party keine Turniere zugewiesen.'),
    ('No match is due right now.', 'Gerade ist keine Begegnung fällig.'),
    ('No match is due at the moment.', 'Keine Begegnung ist aktuell fällig.'),
    ('No match fits these filters.', 'Keine Begegnung passt zu diesen Filtern.'),
    ('Page %(page)d is empty now.', 'Seite %(page)d ist jetzt leer.'),
    ('To page 1', 'Zu Seite 1'),
    # Pin and record.
    ('Pin for the orga team', 'Für das Orga-Team anpinnen'),
    ('Remove pin', 'Pin entfernen'),
    ('Pinned by %(actor)s at %(time)s', 'Angepinnt von %(actor)s um %(time)s'),
    ('Last checked by %(actor)s at %(time)s',
     'Zuletzt geprüft von %(actor)s um %(time)s'),
    ('Delay again – check once more', 'Erneute Verzögerung – nochmals prüfen'),
    ('This match is not available.', 'Diese Begegnung ist nicht verfügbar.'),
])
# fmt: on
def test_design_copy_is_the_german_translation(msgid, german):
    assert _german(msgid) == german


# fmt: off
@pytest.mark.parametrize('code', [
    'Deleted orga',
    dashboard_service.DASHBOARD_UNAUTHENTICATED_ERROR,
    dashboard_service.DASHBOARD_FORBIDDEN_ERROR,
    dashboard_service.DASHBOARD_QUERY_INVALID_ERROR,
    coordination.DASHBOARD_MATCH_NOT_FOUND_ERROR,
    coordination.DASHBOARD_MATCH_TERMINAL_ERROR,
    coordination.DASHBOARD_PIN_CONFLICT_ERROR,
    coordination.DASHBOARD_ACK_CONFLICT_ERROR,
    coordination.DASHBOARD_ACK_COMMENT_INVALID_ERROR,
    coordination.DASHBOARD_ACK_TERMINAL_ERROR,
    coordination.DASHBOARD_ACK_PAUSED_ERROR,
    coordination.DASHBOARD_ACK_NOT_DUE_ERROR,
    coordination.DASHBOARD_ACK_CLOCK_UNKNOWN_ERROR,
    coordination.DASHBOARD_ACK_BELOW_THRESHOLD_ERROR,
    coordination.DASHBOARD_ACK_RECENTLY_ACKNOWLEDGED_ERROR,
    'dashboard_thresholds_stale',
    'lobby_not_free_for_all',
    'lobby_roster_incomplete',
])
# fmt: on
def test_service_error_codes_of_the_epic_have_german(code):
    german = _german(code)

    assert german is not None
    assert german != code
    assert not _placeholders(german)
