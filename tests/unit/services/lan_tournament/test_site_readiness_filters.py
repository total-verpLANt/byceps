"""Actual site contexts and inherited templates; pure DTOs/mock read boundaries.

Fixed batch counts are not SQL instrumentation or browser/mobile assurance.
"""

from dataclasses import replace
from html.parser import HTMLParser
from pathlib import Path
import re
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from flask import Flask, g, get_flashed_messages, request, url_for
from flask_babel import Babel
from jinja2 import Environment, FileSystemLoader, StrictUndefined
import pytest
from werkzeug.exceptions import NotFound

from byceps.services.lan_tournament.blueprints.site import views
from byceps.services.lan_tournament.models.contestant_type import ContestantType
from byceps.services.lan_tournament.models.elimination_mode import EliminationMode
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.match_readiness import ContestantIdentity
from byceps.services.lan_tournament.models.tournament_participant import TournamentParticipant
from byceps.services.lan_tournament.models.tournament_status import TournamentStatus
from byceps.util.result import Err, Ok
from tests.unit.services.lan_tournament.test_readiness_surface_policy import NOW, _row, _tournament


ROOT = Path(__file__).resolve().parents[4]
GENERIC = ROOT / 'byceps/services/lan_tournament/blueprints/site/templates'


@pytest.fixture
def site(monkeypatch):
    app = Flask(__name__)
    app.config.update(TESTING=True, SECRET_KEY='site-projection-test', BABEL_DEFAULT_LOCALE='en')
    Babel(app)
    app.register_blueprint(views.blueprint, url_prefix='/lan-tournaments')
    for endpoint in (
        'user_settings.view', 'user_profile.view', 'dashboard.index',
        'ticketing.index_mine', 'shop_orders.index', 'authn_login.log_in_form',
        'authn_login.log_out', 'authn_login.log_in',
    ):
        app.add_url_rule('/shell/' + endpoint, endpoint, lambda: '', methods=['GET', 'POST'])
    state = SimpleNamespace(
        tournament=_tournament(contestant_type=ContestantType.SOLO),
        rows=[], participants=[], authenticated=False, user_id=uuid4(),
    )
    match_batch = Mock(side_effect=lambda _: [m for m, _, _ in state.rows])
    contestant_batch = Mock(side_effect=lambda _: {m.id: c for m, c, _ in state.rows})
    pairing_batch = Mock(side_effect=lambda ids: {m.id: p for m, _, p in state.rows if m.id in ids and p})
    monkeypatch.setattr(views.tournament_service, 'find_tournament', lambda _: state.tournament)
    monkeypatch.setattr(views.tournament_match_service, 'get_matches_for_tournament_ordered', match_batch)
    monkeypatch.setattr(views.tournament_match_service, 'get_contestants_for_tournament', contestant_batch)
    monkeypatch.setattr(views.tournament_repository, 'get_match_pairings_for_matches', pairing_batch)
    participant_batch = Mock(side_effect=lambda _: state.participants)
    monkeypatch.setattr(views.tournament_participant_service, 'get_participants_for_tournament', participant_batch)
    names = Mock(return_value=({}, {}))
    hover = Mock(return_value=({}, {}))
    monkeypatch.setattr(views, 'build_contestant_name_lookups', names)
    monkeypatch.setattr(views, 'build_hover_lookups', hover)
    monkeypatch.setattr(views, 'build_seat_lookup', lambda *a: {})
    monkeypatch.setattr(views, 'may_administrate_tournament', lambda *a: False)
    monkeypatch.setattr(views.tournament_service, 'resolve_winner_display_name', lambda _: None)
    monkeypatch.setattr(views.tournament_service, 'resolve_podium_display_names', lambda _: {})
    monkeypatch.setattr(views.tournament_orga_service, 'get_public_orgas_for_tournament', lambda _: [])
    monkeypatch.setattr(views.tournament_qualification_service, 'get_qualification', lambda _: Err('qualification_unavailable'))
    actor_batch = Mock(return_value={})
    monkeypatch.setattr(views.user_service, 'get_users_indexed_by_id', actor_batch)
    for name in ('get_match_pairing', 'commit_session', 'rollback_session'):
        monkeypatch.setattr(views.tournament_repository, name, Mock(side_effect=AssertionError('Unexpected read/write: ' + name)))
    monkeypatch.setattr(views.tournament_repository.db, 'session', Mock(spec=[]))

    def context(surface='matches', query='', party='current'):
        with app.test_request_context('/lan-tournaments/' + str(state.tournament.id) + '/' + surface + query):
            g.user = SimpleNamespace(
                id=state.user_id, authenticated=state.authenticated,
                has_permission=lambda _: False,
                as_user=lambda: SimpleNamespace(id=state.user_id, screen_name='Viewer', deleted=False, suspended=False, avatar_url=None),
            )
            g.party = SimpleNamespace(id=state.tournament.party_id if party == 'current' else uuid4())
            g.current_locale = SimpleNamespace(language='en')
            g.site = SimpleNamespace(is_intranet=False)
            return getattr(views, surface).__wrapped__(str(state.tournament.id))

    def render(context_, template='matches', theme='generic'):
        paths = [GENERIC]
        if theme != 'generic':
            paths.insert(0, ROOT / 'sites/totalverplant-36/template_overrides')
        paths.extend(sorted((ROOT / 'byceps/services').glob('*/blueprints/site/templates')))
        paths.extend(sorted((ROOT / 'byceps/services').glob('*/blueprints/common/templates')))
        env = Environment(loader=FileSystemLoader(paths), undefined=StrictUndefined, autoescape=True)
        env.globals.update(
            _=lambda text, **values: text % values if values else text,
            url_for=url_for, url_for_site_file=lambda filename: '/site/' + filename,
            g=g, request=request, now=NOW, get_flashed_messages=get_flashed_messages,
            get_nav_menu_items=lambda _: [],
        )
        env.filters['dateformat'] = lambda value, *a: value.date().isoformat()
        env.filters['timeformat'] = lambda value, *a: value.time().isoformat()
        with app.test_request_context('/lan-tournaments/'):
            g.user = SimpleNamespace(authenticated=False, has_permission=lambda _: False)
            g.party = SimpleNamespace(id=state.tournament.party_id)
            g.current_locale = SimpleNamespace(language='en')
            g.site = SimpleNamespace(is_intranet=False)
            return env.get_template('site/lan_tournament/' + template + '.html').render(**context_)

    state.context = context
    state.render = render
    state.batches = (match_batch, contestant_batch, pairing_batch, participant_batch, names, hover, actor_batch)
    return state


def _personal_rows(site, team=False):
    participant = TournamentParticipant(
        id=uuid4(), user_id=site.user_id, tournament_id=site.tournament.id,
        substitute_player=False, team_id=uuid4() if team else None, created_at=NOW,
    )
    site.participants = [participant]
    site.authenticated = True
    rows = []
    for mine, claims in ((True, 0), (True, 1), (False, 1), (False, 2)):
        match, contestants, pairing = _row(site.tournament, claims=claims)
        if mine:
            contestants[0] = replace(contestants[0], participant_id=None if team else participant.id, team_id=participant.team_id)
            if team:
                contestants[1] = replace(contestants[1], participant_id=None, team_id=uuid4())
            kind = 'team' if team else 'participant'
            pairing = replace(pairing,
                              side_a=ContestantIdentity(kind=kind, id=participant.team_id or participant.id),
                              side_b=ContestantIdentity(kind=kind, id=contestants[1].team_id or contestants[1].participant_id))
        rows.append((match, contestants, pairing))
    site.rows = rows
    return rows


VOID_TAGS = {'meta', 'link', 'img', 'br', 'input', 'hr', 'source'}


class _Node:
    def __init__(self, tag, attrs, parent):
        self.tag = tag
        self.attrs = dict(attrs)
        self.parent = parent
        self.children = []

    @property
    def classes(self):
        return (self.attrs.get('class') or '').split()

    @property
    def text(self):
        return ' '.join(''.join(self._chunks()).split())

    @property
    def own_text(self):
        return ' '.join(''.join(c for c in self.children if isinstance(c, str)).split())

    def _chunks(self):
        for child in self.children:
            if isinstance(child, str):
                yield child
            else:
                yield from child._chunks()

    def find_all(self, cls=None, tag=None):
        for child in _elements(self):
            if (cls is None or cls in child.classes) and (tag is None or child.tag == tag):
                yield child
            yield from child.find_all(cls, tag)


class _Parser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.root = _Node('root', [], None)
        self.current = self.root

    def handle_starttag(self, tag, attrs):
        node = _Node(tag, attrs, self.current)
        self.current.children.append(node)
        if tag not in VOID_TAGS:
            self.current = node

    def handle_startendtag(self, tag, attrs):
        self.current.children.append(_Node(tag, attrs, self.current))

    def handle_endtag(self, tag):
        node = self.current
        while node.parent is not None and node.tag != tag:
            node = node.parent
        if node.parent is not None:
            self.current = node.parent

    def handle_data(self, data):
        self.current.children.append(data)


def _dom(html):
    parser = _Parser()
    parser.feed(html)
    return parser.root


def _elements(node):
    return [child for child in node.children if not isinstance(child, str)]


def _filter_bar(root):
    """Return the single filter nav below `root` and its link items."""
    bars = list(root.find_all('match-filter-bar', 'nav'))
    assert len(bars) == 1
    items = [
        dict(
            key=parse_qs(urlparse(link.attrs['href']).query)['only'][0],
            href=link.attrs['href'],
            label=link.own_text,
            count=int(next(link.find_all('match-filter-bar__count')).text),
            current=link.attrs.get('aria-current'),
            classes=link.classes,
        )
        for link in bars[0].find_all('match-filter-bar__item', 'a')
    ]
    return bars[0], items


# fmt: off
@pytest.mark.parametrize('team', [False, True])
@pytest.mark.parametrize('only,indices', [
    ('all', [0, 1, 2, 3]), ('waiting', []), ('not_ready', [0]),
    ('partially_ready', [1, 2]), ('both_ready', [3]),
    ('no_readiness', [0, 1, 2, 3]), ('finished', []),
])
# fmt: on
def test_site_counts_are_tournament_wide(site, monkeypatch, team, only, indices):
    rows = _personal_rows(site, team)
    counts = Mock(wraps=views.count_match_projections)
    monkeypatch.setattr(views, 'count_match_projections', counts)
    result = site.context(query='?only=' + only)
    counts.assert_called_once()
    assert [p.match_id for p in counts.call_args.args[0]] == [m.id for m, _, _ in rows]
    expected = dict(all=4, waiting=0, not_ready=1, partially_ready=2, both_ready=1, no_readiness=0, finished=0)
    assert result['match_quantities'] == expected
    # The bar hides `no_readiness` at count 0, so asking for it falls back to `all`.
    assert result['only'] == ('all' if only == 'no_readiness' else only)
    assert [entry['match'].id for entry in result['match_data']] == [rows[i][0].id for i in indices]
    assert set(result['readiness_by_match_id']) == {rows[i][0].id for i in indices}
    assert site.batches[4].call_args.args[1] == [entry['contestants'] for entry in result['match_data']]


# fmt: off
@pytest.mark.parametrize('query', ['', '?only=all', '?only=ready', '?only=playable', '?only=open', '?only=unknown'])
# fmt: on
def test_default_and_legacy_filters_show_every_match(site, query):
    rows = _personal_rows(site)
    result = site.context(query=query)
    assert result['only'] == 'all'
    assert [entry['match'].id for entry in result['match_data']] == [m.id for m, _, _ in rows]
    assert result['match_quantities']['all'] == 4


# fmt: off
@pytest.mark.parametrize('surface', ['matches', 'view', 'bracket'])
@pytest.mark.parametrize('refusal', ['draft', 'foreign_party'])
# fmt: on
def test_visibility_refused_before_match_reads(site, surface, refusal):
    if refusal == 'draft':
        site.tournament = replace(site.tournament, tournament_status=TournamentStatus.DRAFT)
    with pytest.raises(NotFound):
        site.context(surface, party='foreign' if refusal == 'foreign_party' else 'current')
    for batch in site.batches:
        batch.assert_not_called()


# fmt: off
@pytest.mark.parametrize('authenticated', [False, True])
# fmt: on
def test_public_nonparticipant_sees_tournament_scope(site, authenticated):
    site.authenticated = authenticated
    site.rows = [_row(site.tournament, claims=n) for n in (0, 1, 2)]
    result = site.context()
    assert len(result['match_data']) == result['match_quantities']['all'] == 3
    quantities = result['match_quantities']
    assert [quantities[key] for key in ('not_ready', 'partially_ready', 'both_ready')] == [1, 1, 1]


def test_default_filter_counts_are_disjoint(site):
    site.rows = [_row(site.tournament, count=count, claims=claims) for count, claims in ((0, 0), (1, 0), (2, 0), (2, 1), (2, 2))]
    result = site.context()
    quantities = result['match_quantities']
    assert result['only'] == 'all'
    assert [entry['match'].id for entry in result['match_data']] == [m.id for m, _, _ in site.rows]
    assert quantities == dict(all=5, waiting=2, not_ready=1, partially_ready=1, both_ready=1, no_readiness=0, finished=0)
    assert sum(count for key, count in quantities.items() if key != 'all') == quantities['all']


# fmt: off
@pytest.mark.parametrize('surface', ['matches', 'view', 'bracket'])
@pytest.mark.parametrize('mode', ['confirmed', 'defwin', 'completed', 'cancelled', 'ffa', 'playoff', 'stale'])
# fmt: on
def test_contexts_use_canonical_format_and_outcome(site, surface, mode):
    if mode in {'completed', 'cancelled'}:
        site.tournament = replace(site.tournament, tournament_status=getattr(TournamentStatus, mode.upper()))
    if mode in {'ffa', 'playoff'}:
        site.tournament = replace(site.tournament, game_format=GameFormat.FREE_FOR_ALL,
                                  playoff_game_format=GameFormat.ONE_V_ONE if mode == 'playoff' else None)
    match, contestants, pairing = _row(site.tournament, claims=2, count=1 if mode == 'defwin' else 2, phase=2 if mode == 'playoff' else 1)
    if mode in {'confirmed', 'defwin'}:
        match = replace(match, confirmed_by=uuid4())
    if mode == 'stale':
        pairing = replace(pairing, generation=3)
    site.rows = [(match, contestants, pairing)]
    result = site.context(surface, query='?only=all')
    projection = result['readiness_by_match_id'][match.id]
    if mode in {'confirmed', 'defwin', 'completed', 'cancelled'}:
        assert projection.outcome == mode
    else:
        assert projection.outcome is None
        assert bool(projection.ready_sides) is (mode == 'playoff')
    assert projection.supports_readiness is (mode != 'ffa')
    assert site.batches[2].call_count == 1
    if surface != 'view':
        assert result['match_data'][0]['readiness'] is projection


# fmt: off
@pytest.mark.parametrize('theme', ['generic', 'totalverplant-36'])
@pytest.mark.parametrize('query,active', [
    ('', 'all'), ('?only=not_ready', 'not_ready'),
    ('?only=finished', 'finished'), ('?only=playable', 'all'),
])
# fmt: on
def test_site_filter_bar_lists_disjoint_buckets(site, theme, query, active):
    _personal_rows(site)
    participant = site.participants[0].id
    html = site.render(site.context(query=query), theme=theme)
    bar, items = _filter_bar(_dom(html))
    assert [i['key'] for i in items] == ['waiting', 'not_ready', 'partially_ready', 'both_ready', 'finished', 'all']
    assert [i['label'] for i in items] == ['Waiting for opponent', 'Not ready', 'Partially ready', 'Both ready', 'Finished', 'All']
    assert [i['count'] for i in items] == [0, 1, 2, 1, 0, 4]
    assert sum(i['count'] for i in items[:-1]) == items[-1]['count']
    assert [i['key'] for i in items if i['current'] is not None] == [active]
    assert [i['current'] for i in items if i['current'] is not None] == ['page']
    assert all(i['classes'] == ['button', 'match-filter-bar__item'] for i in items)
    assert set(re.findall(r'only=(\w+)', html)) == {i['key'] for i in items}
    assert bar.attrs['aria-label'] == 'Filter matches'
    last = _elements(bar)[-1]
    assert last.tag == 'button' and last.attrs['id'] == 'match-filter-mine'
    assert last.classes == ['button', 'match-filter-bar__mine']
    assert last.attrs['aria-pressed'] == 'false'
    assert last.attrs['data-user-participant-id'] == str(participant)
    assert 'data-user-team-id' in last.attrs
    assert last.text == 'My Matches' and _elements(last) == []
    assert len(list(bar.find_all(tag='button'))) == 1


def test_site_filter_bar_has_no_toggle_for_visitors(site):
    site.rows = [_row(site.tournament, claims=1)]
    html = site.render(site.context())
    bar, items = _filter_bar(_dom(html))
    assert list(bar.find_all(tag='button')) == []
    assert 'match-filter-mine' not in html
    assert _elements(bar)[-1].attrs['href'].endswith('only=all')


def test_site_filter_bar_keeps_toggle_when_filter_has_no_rows(site):
    _personal_rows(site)
    result = site.context(query='?only=finished')
    assert result['match_data'] == []
    bar, _ = _filter_bar(_dom(site.render(result)))
    assert [button.attrs['id'] for button in bar.find_all(tag='button')] == ['match-filter-mine']


# fmt: off
@pytest.mark.parametrize('theme', ['generic', 'totalverplant-36'])
# fmt: on
def test_site_hidden_bucket_request_marks_all_current(site, theme):
    site.rows = [_row(site.tournament, claims=claims) for claims in (0, 1, 2)]
    result = site.context(query='?only=no_readiness')
    assert result['only'] == 'all'
    assert [entry['match'].id for entry in result['match_data']] == [
        m.id for m, _, _ in site.rows
    ]
    root = _dom(site.render(result, theme=theme))
    _, items = _filter_bar(root)
    assert 'no_readiness' not in [i['key'] for i in items]
    assert [i['key'] for i in items if i['current'] is not None] == ['all']
    assert len(list(root.find_all('match-link-reset'))) == 3
    assert list(root.find_all('match-list-empty')) == []


def test_site_listed_unsupported_bucket_stays_selected(site):
    site.tournament = replace(
        site.tournament, game_format=GameFormat.FREE_FOR_ALL
    )
    site.rows = [
        _row(site.tournament),
        _row(site.tournament),
        _row(site.tournament, count=1),
    ]
    result = site.context(query='?only=no_readiness')
    assert result['only'] == 'no_readiness'
    assert [entry['match'].id for entry in result['match_data']] == [
        site.rows[0][0].id,
        site.rows[1][0].id,
    ]
    _, items = _filter_bar(_dom(site.render(result)))
    assert [i['key'] for i in items if i['current'] is not None] == [
        'no_readiness'
    ]


def test_site_filter_bar_shows_unsupported_bucket_only_when_present(site):
    site.rows = [_row(site.tournament, claims=0)]
    _, items = _filter_bar(_dom(site.render(site.context())))
    assert 'no_readiness' not in [i['key'] for i in items]
    site.tournament = replace(site.tournament, game_format=GameFormat.FREE_FOR_ALL)
    site.rows = [_row(site.tournament), _row(site.tournament), _row(site.tournament, count=1)]
    _, items = _filter_bar(_dom(site.render(site.context())))
    assert [(i['key'], i['count']) for i in items] == [
        ('waiting', 1), ('not_ready', 0), ('partially_ready', 0), ('both_ready', 0),
        ('no_readiness', 2), ('finished', 0), ('all', 3),
    ]
    assert items[4]['label'] == 'Open (no readiness)'


# fmt: off
@pytest.mark.parametrize('theme', ['generic', 'totalverplant-36'])
@pytest.mark.parametrize('mode,variant,symbol,label,detail', [
    ('empty', 'wait', None, 'Waiting for opponent', None),
    ('one', 'wait', None, 'Waiting for opponent', None),
    ('not_ready', 'none', '○', 'Not ready', None),
    ('side_a', 'part', '◐', 'Partially ready', 'Side A ready · Side B not ready'),
    ('side_b', 'part', '◐', 'Partially ready', 'Side B ready · Side A not ready'),
    ('both', 'both', '✓', 'Both ready', None),
    ('confirmed', 'done', None, 'Confirmed', None),
    ('defwin', 'done', None, 'DEFWIN', None),
    ('completed', 'done', None, 'Completed', None),
    ('cancelled', 'done', None, 'Cancelled', None),
    ('ffa', 'ffa', None, 'Open (no readiness)', None),
    ('ffa_one', 'wait', None, 'Waiting for opponent', None),
])
# fmt: on
def test_site_list_status_badges_follow_design(site, theme, mode, variant, symbol, label, detail):
    if mode in {'completed', 'cancelled'}:
        site.tournament = replace(site.tournament, tournament_status=getattr(TournamentStatus, mode.upper()))
    if mode.startswith('ffa'):
        site.tournament = replace(site.tournament, game_format=GameFormat.FREE_FOR_ALL)
    count = 0 if mode == 'empty' else 1 if mode in {'one', 'defwin', 'ffa_one'} else 2
    claims = 1 if mode in {'side_a', 'side_b'} else 2 if mode in {'both', 'confirmed', 'defwin', 'completed', 'cancelled', 'ffa'} else 0
    match, contestants, pairing = _row(site.tournament, count=count, claims=claims)
    if mode == 'side_b':
        match = replace(match, ready_at_a=None, ready_at_b=NOW)
    if mode in {'confirmed', 'defwin'}:
        match = replace(match, confirmed_by=uuid4())
    site.rows = [(match, list(reversed(contestants)), pairing)]
    html = site.render(site.context(query='?only=all'), theme=theme)
    cells = list(_dom(html).find_all('match-list-status'))
    assert len(cells) == 1
    badges = list(cells[0].find_all('readiness-badge'))
    assert len(badges) == 1
    assert badges[0].classes == ['readiness-badge', 'readiness-badge--' + variant]
    assert badges[0].own_text == label
    symbols = [s.text for s in badges[0].find_all(tag='span') if s.attrs.get('aria-hidden') == 'true']
    assert symbols == ([symbol] if symbol else [])
    details = list(cells[0].find_all('readiness-badge__detail'))
    assert [(d.tag, d.text) for d in details] == ([('small', detail)] if detail else [])
    assert 'ready/claim' not in html and 'ready/revoke' not in html


def test_site_list_has_one_header_row_before_the_rows(site):
    site.rows = [_row(site.tournament, claims=n) for n in (0, 1)]
    dom = _dom(site.render(site.context(query='?only=all')))
    table = next(dom.find_all('match-table'))
    heads = list(table.find_all('match-row--head'))
    assert len(heads) == 1
    assert [s.text for s in _elements(heads[0])] == ['Match', 'Participants', 'Result', 'Status']
    assert _elements(table)[0] is heads[0]
    rows = _elements(table)[1:]
    assert len(rows) == 2
    assert all(row.tag == 'a' and 'match-link-reset' in row.classes for row in rows)
    assert list(dom.find_all('match-list-empty')) == []


# fmt: off
@pytest.mark.parametrize('query,rows,message', [
    ('', 0, 'No matches have been created yet.'),
    ('?only=all', 0, 'No matches have been created yet.'),
    ('?only=both_ready', 1, 'No matches with this readiness state.'),
    ('?only=finished', 1, 'No matches with this readiness state.'),
])
# fmt: on
def test_site_list_empty_states_follow_design(site, query, rows, message):
    site.rows = [_row(site.tournament, claims=0) for _ in range(rows)]
    dom = _dom(site.render(site.context(query=query)))
    empties = list(dom.find_all('match-list-empty'))
    assert [(e.tag, e.text) for e in empties] == [('p', message)]
    assert list(dom.find_all('match-link-reset')) == []
    assert len(list(dom.find_all('match-row--head'))) == 1


# fmt: off
@pytest.mark.parametrize('theme', ['generic', 'totalverplant-36'])
@pytest.mark.parametrize('status', [TournamentStatus.ONGOING, TournamentStatus.COMPLETED, TournamentStatus.CANCELLED])
# fmt: on
def test_real_overview_counts_and_terminal_delete_hidden(site, theme, status):
    site.tournament = replace(site.tournament, tournament_status=status)
    site.rows = [_row(site.tournament, claims=n) for n in (0, 1, 2)]
    context = site.context('view')
    if status in {TournamentStatus.COMPLETED, TournamentStatus.CANCELLED}:
        context['may_administrate'] = True
    html = site.render(context, 'view', theme)
    assert 'only=waiting' in html and 'only=both_ready' in html
    assert 'only=playable' not in html and 'only=open' not in html
    assert 'Delete' not in html
    ongoing = status is TournamentStatus.ONGOING
    assert context['match_quantities']['both_ready'] == (1 if ongoing else 0)
    assert context['match_quantities']['finished'] == (0 if ongoing else 3)


# fmt: off
@pytest.mark.parametrize('theme', ['generic', 'totalverplant-36'])
# fmt: on
def test_overview_nav_has_matches_label(site, theme):
    _personal_rows(site)
    html = site.render(site.context('view'), 'view', theme)
    wrappers = list(_dom(html).find_all('match-filter-nav'))
    assert len(wrappers) == 1
    labels = list(wrappers[0].find_all('match-filter-nav__label'))
    assert [(label.tag, label.text) for label in labels] == [('span', 'Matches')]
    bar, items = _filter_bar(wrappers[0])
    assert bar.attrs['aria-label'] == 'Filter matches'
    assert [(i['key'], i['count']) for i in items] == [
        ('waiting', 0), ('not_ready', 1), ('partially_ready', 2),
        ('both_ready', 1), ('finished', 0), ('all', 4),
    ]
    assert all(i['href'].endswith('/matches?only=' + i['key']) for i in items)
    assert [i['key'] for i in items if i['current'] is not None] == ['all']
    assert list(wrappers[0].find_all(tag='button')) == []
    assert 'match-filter-mine' not in html


def test_overview_counts_match_list(site):
    _personal_rows(site)
    overview = site.context('view')
    matches = site.context()
    assert overview['match_quantities'] == matches['match_quantities']
    assert overview['match_filter_options'] == matches['match_filter_options']
    assert overview['match_quantities']['both_ready'] == 1


def test_bracket_serializer_receives_canonical_projection(site):
    site.tournament = replace(site.tournament, elimination_mode=EliminationMode.SINGLE_ELIMINATION)
    site.rows = [_row(site.tournament, claims=2)]
    result = site.context('bracket')
    payload = result['bracket_json']
    assert 'both_ready' in str(payload)
    assert 'Both ready' in str(payload)
    assert 'ready_by_a' not in str(payload) and 'pairing_generation' not in str(payload)


def test_playoff_bracket_serializer_receives_effective_projection(site, monkeypatch):
    site.tournament = replace(site.tournament, game_format=GameFormat.FREE_FOR_ALL,
                              playoff_game_format=GameFormat.ONE_V_ONE,
                              playoff_elimination_mode=EliminationMode.SINGLE_ELIMINATION)
    site.rows = [_row(site.tournament, phase=2, claims=2)]
    state = SimpleNamespace(source='leaderboard', released_at=NOW)
    monkeypatch.setattr(views.tournament_qualification_service, 'get_qualification', lambda _: Ok(state))
    monkeypatch.setattr(views, 'participant_rankings', lambda *a: [])
    monkeypatch.setattr(views, 'contestant_names', lambda *a: {})
    monkeypatch.setattr(views, 'playoff_waiting_reason', lambda *a: None)
    monkeypatch.setattr(views, 'playoff_origin_labels', lambda *a: {})
    result = site.context('bracket')
    assert result['phase_view'] == 2
    assert 'both_ready' in str(result['bracket_json'])
    assert result['readiness_by_match_id'][site.rows[0][0].id].supports_readiness


# fmt: off
@pytest.mark.parametrize('surface', ['matches', 'view', 'bracket'])
# fmt: on
def test_projection_batch_query_growth(site, surface):
    observed = []
    for quantity in (10, 100):
        for batch in site.batches:
            batch.reset_mock()
        site.rows = [_row(site.tournament, claims=2) for _ in range(quantity)]
        result = site.context(surface, query='?only=all')
        assert len(result['readiness_by_match_id']) == quantity
        for batch in site.batches[:4]:
            assert batch.call_count == 1
        site.batches[2].assert_called_once_with([m.id for m, _, _ in site.rows])
        observed.append(tuple(batch.call_count for batch in site.batches))
    assert observed[0] == observed[1]
    expected = (1, 1, 1, 1, 0, 0, 1) if surface == 'view' else (1, 1, 1, 1, 1, 1, 0)
    assert observed == [expected, expected]
