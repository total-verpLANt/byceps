"""Actual native GET context, WTForms widgets and StrictUndefined templates.

Repositories/role resolution are mocked, not forms, projection or templates.
This is no-DB rendering evidence, not browser/mobile or live HTTP-role proof.
"""

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
import re
from types import SimpleNamespace
from uuid import uuid4

from flask import Flask, g, get_flashed_messages, request as flask_request, url_for
from jinja2 import Environment, FileSystemLoader, StrictUndefined
import pytest
from werkzeug.datastructures import MultiDict

from byceps.services.lan_tournament.blueprints.readiness_forms import (
    MatchReadyClaimForm,
    MatchReadyRevokeForm,
)
from byceps.services.lan_tournament.blueprints.site import views
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.match_readiness import (
    ContestantIdentity,
    MatchPairing,
)
from byceps.services.lan_tournament.models.tournament_match import (
    MatchSide,
    MatchUserRole,
    TournamentMatch,
)
from byceps.services.lan_tournament.models.tournament_match_to_contestant import (
    TournamentMatchToContestant,
)
from byceps.services.lan_tournament.models.tournament_status import TournamentStatus
from byceps.util.result import Ok


ROOT = Path(__file__).resolve().parents[4]
GENERIC = ROOT / 'byceps/services/lan_tournament/blueprints/site/templates'
THEME = ROOT / 'sites/totalverplant-36/template_overrides'
PARTIAL = 'site/lan_tournament/_match_readiness.html'
DETAIL = 'site/lan_tournament/view_match.html'
NOW = datetime(2026, 10, 5, 12, tzinfo=UTC)


@dataclass(frozen=True)
class Person:
    id: object
    screen_name: str
    deleted: bool = False


class DOM(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.forms = []
        self.elements = []
        self.current = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        self.elements.append((tag, attributes))
        if tag == 'form':
            self.current = {'attributes': attributes, 'inputs': {}, 'text': ''}
            self.forms.append(self.current)
        elif tag == 'input' and self.current is not None:
            self.current['inputs'][attributes['name']] = attributes
        elif tag == 'button' and self.current is not None:
            self.current['button'] = attributes

    def handle_endtag(self, tag):
        if tag == 'form':
            self.current = None

    def handle_data(self, data):
        if self.current is not None:
            self.current['text'] += data

    @property
    def readiness_forms(self):
        return [f for f in self.forms if 'readiness-' in f['attributes'].get('class', '')]


# fmt: off
@pytest.fixture(params=['generic', 'totalverplant-36'])
# fmt: on
def render_readiness(request, monkeypatch):
    app = Flask(__name__)
    app.config.update(TESTING=True, LOCALE='en', SECRET_KEY='render-test-only')
    app.register_blueprint(views.blueprint, url_prefix='/lan-tournaments')
    # Shell-only endpoints are outside this unit app. All tournament POST URLs
    # continue to use the actual registered blueprint, never a URL stub.
    for endpoint in (
        'user_settings.view', 'user_profile.view', 'dashboard.index',
        'ticketing.index_mine', 'shop_orders.index', 'authn_login.log_in_form',
        'authn_login.log_out', 'authn_login.log_in',
    ):
        app.add_url_rule('/shell/' + endpoint, endpoint, lambda: '', methods=['GET', 'POST'])
    search_paths = [GENERIC]
    if request.param == 'totalverplant-36':
        search_paths.insert(0, THEME)
    search_paths.extend(sorted((ROOT / 'byceps/services').glob('*/blueprints/site/templates')))
    search_paths.extend(sorted((ROOT / 'byceps/services').glob('*/blueprints/common/templates')))
    env = Environment(loader=FileSystemLoader(search_paths), undefined=StrictUndefined, autoescape=True)
    env.globals.update(
        _=lambda message, **values: message % values if values else message,
        url_for=url_for,
        url_for_site_file=lambda filename: '/site/' + filename,
        g=g, request=flask_request, now=NOW,
        get_flashed_messages=get_flashed_messages,
        get_nav_menu_items=lambda name: [],
    )
    env.filters['dateformat'] = lambda value: value.date().isoformat()
    env.filters['timeformat'] = lambda value, *args: value.time().isoformat()
    assert Path(env.get_template(PARTIAL).filename) == GENERIC / PARTIAL
    assert Path(env.get_template(DETAIL).filename) == GENERIC / DETAIL

    def render(*, role='solo', ready=(), status=TournamentStatus.ONGOING,
               format_=GameFormat.ONE_V_ONE, assigned=2, confirmed=False,
               reverse=False, stale=False, full=False, score=False, name=None,
               reread_mismatch=False):
        match = TournamentMatch(
            id=uuid4(), tournament_id=uuid4(), group_order=None,
            match_order=1, round=1, next_match_id=None, confirmed_by=None,
            created_at=NOW, occupied_since=NOW - timedelta(days=2),
            pairing_id=uuid4(), pairing_generation=3, readiness_revision=7,
        )
        user_id = uuid4()
        match = replace(match, confirmed_by=user_id if confirmed else None,
                        ready_at_a=NOW if MatchSide.A in ready else None,
                        ready_at_b=NOW + timedelta(minutes=1) if MatchSide.B in ready else None)
        team = role in {'captain', 'member', 'member_orga'}
        identities = tuple(ContestantIdentity(kind='team' if team else 'participant', id=uuid4()) for _ in range(max(2, assigned)))
        pairing = MatchPairing(
            id=match.pairing_id, match_id=match.id, tournament_id=match.tournament_id,
            generation=3, side_a=identities[0], side_b=identities[1], started_at=NOW - timedelta(hours=1),
        )
        contestants = [TournamentMatchToContestant(
            id=uuid4(), tournament_match_id=match.id,
            participant_id=None if team else identity.id,
            team_id=identity.id if team else None, score=index + 1,
            created_at=NOW + timedelta(seconds=index),
            placement=index + 1 if confirmed and format_ is GameFormat.FREE_FOR_ALL else None,
            points=10 - index,
        ) for index, identity in enumerate(identities)][:assigned]
        if reverse:
            contestants.reverse()
        people = {i.id: Person(i.id, name or f'Player {index}') for index, i in enumerate(identities)}
        teams = {i.id: SimpleNamespace(id=i.id, name=name or f'Team {index}') for index, i in enumerate(identities)} if team else {}
        tournament = SimpleNamespace(
            id=match.tournament_id, party_id=uuid4(), name='Render Cup',
            tournament_status=status, game_format=format_, has_playoffs=False,
            contestant_type=None, elimination_mode=None,
        )
        is_orga = role in {'scoped_orga', 'global_orga', 'member_orga'}
        sides = set(MatchSide) if is_orga else {MatchSide.A} if role in {'solo', 'captain'} else set()
        repo = views.tournament_repository
        monkeypatch.setattr(views.tournament_match_service, 'get_match', lambda _: match)
        monkeypatch.setattr(views.tournament_match_service, 'get_contestants_for_match', lambda _: contestants)
        monkeypatch.setattr(views.tournament_match_service, 'get_comments_from_match', lambda _: [])
        monkeypatch.setattr(views.tournament_match_service, 'get_user_match_role', lambda *a, **kw: MatchUserRole(contestant=None, is_loser=score, can_confirm=False, can_submit=score))
        monkeypatch.setattr(views.tournament_service, 'find_tournament', lambda _: tournament)
        monkeypatch.setattr(views, 'build_contestant_name_lookups', lambda *a: (teams, people))
        monkeypatch.setattr(views, 'build_hover_lookups', lambda *a: ({}, {}))
        monkeypatch.setattr(views, 'may_administrate_tournament', lambda *a: False)
        monkeypatch.setattr(views, 'get_permissions_for_user', lambda _: {'lan_tournament.administrate'} if role == 'global_orga' else set())
        monkeypatch.setattr(views.tournament_orga_service, 'is_orga_for_tournament', lambda *a: role in {'scoped_orga', 'member_orga'})
        monkeypatch.setattr(views.tournament_readiness_authorization_service, 'get_user_readiness_sides', lambda *a: Ok(frozenset(sides)))
        actual_pairing = replace(pairing, generation=2) if stale else pairing
        reread_pairing = replace(actual_pairing, id=uuid4()) if reread_mismatch else actual_pairing
        monkeypatch.setattr(repo, 'get_match_pairing', lambda _: reread_pairing)
        monkeypatch.setattr(repo, 'get_match_pairings_for_matches', lambda _: {match.id: actual_pairing} if assigned == 2 else {})
        monkeypatch.setattr(views.user_service, 'get_users_indexed_by_id', lambda _: {})
        with app.test_request_context('/lan-tournaments/matches/' + str(match.id)):
            user = SimpleNamespace(id=user_id, screen_name='Viewer', deleted=False,
                                   suspended=False, avatar_url=None)
            g.user = SimpleNamespace(id=user_id, authenticated=role != 'anonymous',
                                     has_permission=lambda _: False, as_user=lambda: user)
            g.current_locale = SimpleNamespace(language='en')
            g.site = SimpleNamespace(is_intranet=False)
            g.party = SimpleNamespace(id=tournament.party_id)
            context = views.view_match.__wrapped__(str(match.id))
            assert isinstance(context['claim_form'], MatchReadyClaimForm)
            assert isinstance(context['revoke_form'], MatchReadyRevokeForm)
            assert context['claim_form'].expected_pairing_generation.data == 3
            assert context['claim_form'].expected_readiness_revision.data == 7
            html = env.get_template(DETAIL if full else PARTIAL).render(**context)
            return SimpleNamespace(html=html, dom=DOM(html), context=context,
                                   app=app, env=env, pairing=pairing, people=people)

    return render


def test_solo_renders_one_own_claim_form(render_readiness):
    rendered = render_readiness()
    forms = rendered.dom.readiness_forms
    assert len(forms) == 1
    form = forms[0]
    assert form['attributes']['method'] == 'post'
    assert form['attributes']['action'].endswith('/ready/claim')
    assert form['inputs']['side']['value'] == 'a'
    assert 'I am ready' in form['text'] and 'on behalf' not in form['text']


# fmt: off
@pytest.mark.parametrize('role,count,on_behalf', [
    ('solo', 1, False), ('captain', 1, False), ('member', 0, False),
    ('scoped_orga', 2, True), ('global_orga', 2, True), ('member_orga', 2, True),
    ('outsider', 0, False), ('anonymous', 0, False),
])
@pytest.mark.parametrize('ready', [(), (MatchSide.A,), (MatchSide.B,), tuple(MatchSide)])
# fmt: on
def test_captain_and_member_controls(render_readiness, role, count, on_behalf, ready):
    rendered = render_readiness(role=role, ready=ready)
    forms = rendered.dom.readiness_forms
    assert len(forms) == count
    expected_sides = {'a', 'b'} if count == 2 else {'a'} if count else set()
    assert {f['inputs']['side']['value'] for f in forms} == expected_sides
    for form in forms:
        side = MatchSide(form['inputs']['side']['value'])
        action = 'revoke' if side in ready else 'claim'
        assert form['attributes']['action'].endswith('/ready/' + action)
        assert ('on behalf of this side' in form['text']) is on_behalf
    ids = [attrs['id'] for _, attrs in rendered.dom.elements if 'id' in attrs]
    assert len(ids) == len(set(ids))


def test_scoped_orga_renders_two_on_behalf_forms(render_readiness):
    forms = render_readiness(role='scoped_orga').dom.readiness_forms
    assert len(forms) == 2
    assert all('Ready (on behalf of this side)' in f['text'] for f in forms)
    assert {f['attributes']['aria-label'] for f in forms} == {'Side A', 'Side B'}


def test_unready_form_has_revision_and_token_only(render_readiness):
    rendered = render_readiness(ready=(MatchSide.A,))
    forms = rendered.dom.readiness_forms
    assert len(forms) == 1
    form = forms[0]
    assert form['attributes']['action'].endswith('/ready/revoke')
    fields = form['inputs']
    assert set(fields) == {
        'side',
        'csrf_token',
        'expected_pairing_generation',
        'expected_readiness_revision',
    }
    for field, value in {
        'side': 'a',
        'expected_pairing_generation': '3',
        'expected_readiness_revision': '7',
        'csrf_token': rendered.context['readiness_csrf_token'],
    }.items():
        assert fields[field]['type'] == 'hidden'
        assert fields[field]['value'] == value
    assert not any(
        tag in {'label', 'textarea'} for tag, _ in rendered.dom.elements
    )
    assert 'reason' not in rendered.html.lower()
    with rendered.app.app_context():
        real_form = MatchReadyRevokeForm(
            MultiDict({key: field['value'] for key, field in fields.items()})
        )
        assert real_form.validate(), real_form.errors


def _state_line(html):
    classes, inner = re.search(
        r'<p class="(readiness-state[^"]*)">(.*?)</p>', html, re.S
    ).groups()
    return (
        classes.split(),
        inner,
        ' '.join(re.sub(r'<[^>]+>', ' ', inner).split()),
    )


# fmt: off
@pytest.mark.parametrize('options,modifier,symbol,text', [
    ({}, 'none', '○', 'Not ready'),
    ({'ready': (MatchSide.A,)}, 'part', '◐', 'Partially ready — Side A ready'),
    ({'ready': (MatchSide.B,)}, 'part', '◐', 'Partially ready — Side B ready'),
    ({'ready': tuple(MatchSide)}, 'both', '✓', 'Both ready'),
])
# fmt: on
def test_card_state_symbols_and_side_states_follow_design(render_readiness, options, modifier, symbol, text):
    rendered = render_readiness(**options)
    classes, inner, label = _state_line(rendered.html)
    assert classes == ['readiness-state', f'readiness-state--{modifier}']
    assert f'<span class="readiness-state__sym" aria-hidden="true">{symbol}</span>' in inner
    assert label == f'{symbol} {text}'
    side_classes = dict((side, css.split()) for css, side in re.findall(r'<li class="([^"]*)" data-side="([ab])"', rendered.html))
    assert set(side_classes) == {'a', 'b'}
    for side in 'ab':
        ready = MatchSide(side) in options.get('ready', ())
        side_html = rendered.html.split(f'data-side="{side}"', 1)[1].split('</li>', 1)[0]
        mark, word = ('✓', 'Ready') if ready else ('○', 'Not ready')
        assert f'<span class="readiness-side__state"><span aria-hidden="true">{mark}</span> {word}</span>' in side_html
        assert ('Ready since' in side_html) is ready
        assert ('readiness-side--ready' in side_classes[side]) is ready
        # The side label sits above the name, then the state, then the time.
        label_at = side_html.index(f'<span class="readiness-side__label">Side {side.upper()}</span>')
        name_at = side_html.index('class="readiness-side__name"')
        state_at = side_html.index('class="readiness-side__state"')
        assert label_at < name_at < state_at
        assert f'Side {side.upper()}:' not in side_html
        if ready:
            assert state_at < side_html.index('class="readiness-side__since"')


# fmt: off
@pytest.mark.parametrize('assigned', [0, 1])
# fmt: on
def test_waiting_state_has_no_symbol_note_times_or_sides(render_readiness, assigned):
    rendered = render_readiness(role='global_orga', assigned=assigned, ready=tuple(MatchSide))
    classes, _, label = _state_line(rendered.html)
    assert classes == ['readiness-state', 'readiness-state--wait']
    assert label == 'Waiting for opponent'
    assert 'aria-hidden' not in rendered.html
    for absent in ('readiness-hint', 'readiness-occupied-since', 'readiness-sides', 'data-side="'):
        assert absent not in rendered.html
    assert not rendered.dom.readiness_forms


def test_claim_button_is_highlighted_unready_is_plain(render_readiness):
    solo_claim = render_readiness().dom.readiness_forms
    assert [f['button']['class'].split() for f in solo_claim] == [['button', 'readiness-btn', 'readiness-btn--claim']]
    solo_unready = render_readiness(ready=(MatchSide.A,)).dom.readiness_forms
    assert [f['button']['class'].split() for f in solo_unready] == [['button', 'secondary', 'readiness-btn']]
    orga = render_readiness(role='scoped_orga', ready=(MatchSide.A,)).dom.readiness_forms
    classes = {f['inputs']['side']['value']: f['button']['class'].split() for f in orga}
    assert classes['a'] == ['button', 'secondary', 'readiness-btn']
    assert classes['b'] == ['button', 'readiness-btn', 'readiness-btn--claim']


# fmt: off
@pytest.mark.parametrize('role,ready,label', [
    ('solo', (MatchSide.A,), 'I am not ready'),
    ('captain', (MatchSide.A,), 'I am not ready'),
    ('scoped_orga', tuple(MatchSide), 'Not ready (on behalf of this side)'),
    ('global_orga', tuple(MatchSide), 'Not ready (on behalf of this side)'),
])
# fmt: on
def test_unready_button_labels_for_player_and_orga(render_readiness, role, ready, label):
    forms = render_readiness(role=role, ready=ready).dom.readiness_forms
    assert len(forms) == len(ready)
    for form in forms:
        assert form['attributes']['action'].endswith('/ready/revoke')
        assert ' '.join(form['text'].split()) == label


# fmt: off
@pytest.mark.parametrize('options,hint', [
    ({'confirmed': True}, 'Confirmed'),
    ({'format_': GameFormat.FREE_FOR_ALL}, 'Readiness is not available for this match format.'),
    ({'assigned': 0}, 'Waiting for opponent'),
    ({'assigned': 1}, 'Waiting for opponent'),
    ({'status': TournamentStatus.PAUSED}, 'Tournament paused; readiness is read-only.'),
    ({'status': TournamentStatus.COMPLETED}, 'Tournament ended; readiness is read-only.'),
    ({'status': TournamentStatus.CANCELLED}, 'Tournament ended; readiness is read-only.'),
    ({'stale': True}, 'Current pairing is unavailable; readiness is read-only.'),
])
# fmt: on
def test_terminal_and_unsupported_have_no_mutations(render_readiness, options, hint):
    rendered = render_readiness(role='global_orga', ready=tuple(MatchSide), **options)
    assert not rendered.dom.readiness_forms
    assert hint in rendered.html


# fmt: off
@pytest.mark.parametrize('assigned,ready,label', [
    (0, (), 'Waiting for opponent'), (1, (), 'Waiting for opponent'),
    (2, (), 'Not ready'),
    (2, (MatchSide.A,), 'Partially ready'), (2, (MatchSide.B,), 'Partially ready'),
    (2, tuple(MatchSide), 'Both ready'),
])
# fmt: on
def test_four_states_distinguish_assignment_from_claims(render_readiness, assigned, ready, label):
    rendered = render_readiness(assigned=assigned, ready=ready)
    assert label in rendered.html
    if len(ready) == 1:
        assert ('Side A' if ready[0] is MatchSide.A else 'Side B') + ' ready' in rendered.html
    # The wait state carries no symbol; every other state does.
    assert any(attrs.get('aria-hidden') == 'true' for _, attrs in rendered.dom.elements) is (label != 'Waiting for opponent')


def test_original_current_and_ready_timestamps_are_separate(render_readiness):
    rendered = render_readiness(ready=tuple(MatchSide))
    card = next(attrs for tag, attrs in rendered.dom.elements if tag == 'section')
    assert card['data-original-occupied-since'] == (NOW - timedelta(days=2)).isoformat()
    assert card['data-pairing-started-at'] == (NOW - timedelta(hours=1)).isoformat()
    assert card['data-side-a-ready-at'] == NOW.isoformat()
    assert card['data-side-b-ready-at'] == (NOW + timedelta(minutes=1)).isoformat()
    assert len([attrs for tag, attrs in rendered.dom.elements if tag == 'time' and 'datetime' in attrs]) == 4


def test_names_are_escaped_and_private_facts_are_absent(render_readiness):
    rendered = render_readiness(name='<script>alert("x")</script>')
    assert '<script>' not in rendered.html
    assert '&lt;script&gt;' in rendered.html
    assert 'ready_by' not in rendered.html and 'history' not in rendered.html


def test_inherited_detail_preserves_above_score_controls(render_readiness):
    rendered = render_readiness(full=True, score=True)
    assert len(rendered.dom.readiness_forms) == 1
    assert rendered.html.index('readiness-card') < rendered.html.index('score-submit-card')
    score_form = next(f for f in rendered.dom.forms if 'score-submit-form' in f['attributes'].get('class', ''))
    assert score_form['attributes']['action'].endswith('/set_score')
    assert any(attrs.get('max') == '999999999' for tag, attrs in rendered.dom.elements if tag == 'input')


# fmt: off
@pytest.mark.parametrize('options,expected', [
    ({'confirmed': True}, 'Confirmed'),
    ({'confirmed': True, 'assigned': 1}, 'DEFWIN'),
    ({'format_': GameFormat.FREE_FOR_ALL, 'assigned': 3, 'confirmed': True}, 'Placements'),
    ({'status': TournamentStatus.PAUSED}, 'Tournament paused; readiness is read-only.'),
    ({'status': TournamentStatus.COMPLETED}, 'Tournament ended; readiness is read-only.'),
    ({'status': TournamentStatus.CANCELLED}, 'Tournament ended; readiness is read-only.'),
])
# fmt: on
def test_inherited_detail_preserves_outcomes(render_readiness, options, expected):
    rendered = render_readiness(full=True, role='global_orga', **options)
    assert expected in rendered.html
    assert not rendered.dom.readiness_forms
    if options.get('format_') is GameFormat.FREE_FOR_ALL:
        assert 'podium__card--1st' in rendered.html
        assert 'Readiness is not available' not in rendered.html


def test_inherited_detail_escapes_labels_and_keeps_native_forms(render_readiness):
    rendered = render_readiness(full=True, name='<img src=x onerror=alert(1)>')
    assert '<img src=x onerror=alert(1)>' not in rendered.html
    assert '&lt;img src=x onerror=alert(1)&gt;' in rendered.html
    form = rendered.dom.readiness_forms[0]
    assert form['attributes']['method'] == 'post'
    assert 'onsubmit' not in form['attributes']
    assert len(form['inputs']) == 4


def test_reseated_pair_keeps_side_identity_in_actual_context(render_readiness):
    # Canonical repository may reseat rows without changing logical A/B.
    # A correct control must still identify the original side-A contestant.
    rendered = render_readiness(reverse=True, ready=(MatchSide.A,))
    html = rendered.html
    side_a = html.split('data-side="a"', 1)[1].split('</li>', 1)[0]
    assert rendered.people[rendered.pairing.side_a.id].screen_name in side_a


# fmt: off
@pytest.mark.parametrize('role', ['solo', 'captain', 'scoped_orga', 'member_orga'])
# fmt: on
def test_reseated_readiness_map_preserves_score_row_order(render_readiness, role):
    rendered = render_readiness(role=role, reverse=True, full=True, score=True,
                               ready=(MatchSide.A,))
    mapping = rendered.context['readiness_contestants_by_side']
    assert set(mapping) == set(MatchSide)
    assert rendered.context['contestants'] == [mapping[MatchSide.B], mapping[MatchSide.A]]
    assert [attrs['value'] for tag, attrs in rendered.dom.elements
            if tag == 'input' and attrs.get('name') == 'score'] == ['2', '1']
    mapped_sides = [attrs['data-side'] for tag, attrs in rendered.dom.elements
                    if tag == 'li' and 'data-side' in attrs]
    assert mapped_sides == ['a', 'b']
    side_a = rendered.html.split('data-side="a"', 1)[1].split('</li>', 1)[0]
    side_b = rendered.html.split('data-side="b"', 1)[1].split('</li>', 1)[0]
    prefix = 'Team' if role in {'captain', 'member_orga'} else 'Player'
    assert f'{prefix} 0' in side_a and f'{prefix} 1' not in side_a
    assert f'{prefix} 1' in side_b and f'{prefix} 0' not in side_b
    for form in rendered.dom.readiness_forms:
        fields = form['inputs']
        side = fields['side']['value']
        assert form['attributes']['action'].endswith('/ready/revoke' if side == 'a' else '/ready/claim')
        assert fields['expected_pairing_generation']['value'] == '3'
        assert fields['expected_readiness_revision']['value'] == '7'


# fmt: off
@pytest.mark.parametrize('options,label', [
    ({'assigned': 0}, 'Waiting for opponent'),
    ({'assigned': 1}, 'Waiting for opponent'),
    ({'stale': True}, 'Current pairing is unavailable; readiness is read-only.'),
    ({'reread_mismatch': True}, 'Current pairing is unavailable; readiness is read-only.'),
    ({'format_': GameFormat.FREE_FOR_ALL}, 'Readiness is not available for this match format.'),
])
# fmt: on
def test_empty_side_map_has_no_manufactured_sides_or_ready_labels(render_readiness, options, label):
    rendered = render_readiness(role='global_orga', ready=tuple(MatchSide), **options)
    assert rendered.context['readiness_contestants_by_side'] == {}
    assert not rendered.dom.readiness_forms
    assert not any('data-side' in attrs for _, attrs in rendered.dom.elements)
    assert label in rendered.html
    assert 'Both ready' not in rendered.html and 'Partially ready' not in rendered.html
    assert 'readiness-sides' not in rendered.html


# fmt: off
@pytest.mark.parametrize('options', [
    {'status': TournamentStatus.PAUSED}, {'confirmed': True},
])
# fmt: on
def test_readonly_verified_pair_keeps_logical_side_labels(render_readiness, options):
    rendered = render_readiness(role='global_orga', reverse=True,
                               ready=(MatchSide.A,), **options)
    assert set(rendered.context['readiness_contestants_by_side']) == set(MatchSide)
    assert not rendered.dom.readiness_forms
    side_a = rendered.html.split('data-side="a"', 1)[1].split('</li>', 1)[0]
    side_b = rendered.html.split('data-side="b"', 1)[1].split('</li>', 1)[0]
    assert 'Player 0' in side_a and 'Player 1' not in side_a
    assert 'Player 1' in side_b and 'Player 0' not in side_b
    assert NOW.isoformat() in side_a


def test_readiness_css_is_scoped_and_keyboard_touch_friendly():
    css = (ROOT / 'byceps/static/style/lan_tournament.css').read_text()
    rules = css.split('/* ── F-04: Match readiness card', 1)[1]
    assert '.readiness-card .readiness-btn:focus-visible' in rules
    assert 'outline: 2px solid' in rules and 'outline-offset: 2px' in rules
    assert 'readiness-revoke-input' not in css and 'readiness-history' not in css
    assert 'min-height: 44px' in rules and 'min-width: 44px' in rules
    assert 'overflow-wrap: anywhere' in rules and 'white-space: normal' in rules
    assert 'min-width: 0' in rules and 'box-sizing: border-box' in rules


def _rule(css, selector):
    match = re.search(
        r'(?m)^' + re.escape(selector) + r' \{\n(.*?)\n\}', css, re.S
    )
    assert match, selector
    return match.group(1)


def test_card_css_uses_site_tokens_with_module_fallbacks():
    css = (ROOT / 'byceps/static/style/lan_tournament.css').read_text()
    card = css.split('/* ── F-04: Match readiness card', 1)[1].split(
        '\n/* ', 1
    )[0]
    green = 'var(--green, var(--lt-brand-success))'
    assert f'color: {green};' in _rule(card, '.readiness-state--both')
    assert f'color: {green};' in _rule(
        card, '.readiness-side--ready .readiness-side__state'
    )
    claim = _rule(card, '.readiness-card .readiness-btn--claim')
    assert (
        f'background: {green};' in claim and f'border-color: {green};' in claim
    )
    assert 'color: var(--paperLite, var(--lt-ink));' in claim
    focus = _rule(card, '.readiness-card .readiness-btn:focus-visible')
    assert 'outline: 2px solid var(--redDeep, var(--lt-brand-danger));' in focus
    button = _rule(card, '.readiness-card .readiness-btn')
    for declaration in (
        'border: 1.5px solid var(--lt-text);',
        'font-family: var(--mono, monospace);',
        'letter-spacing: 0.18em;',
        'text-transform: uppercase;',
    ):
        assert declaration in button
    assert 'font-family: var(--display, inherit);' in _rule(
        card, '.readiness-state'
    )
    assert 'border: 1.5px solid var(--lt-text);' in _rule(
        card, '.readiness-card'
    )
    who = _rule(card, '.readiness-side__who')
    assert 'flex: 1 1 15rem;' in who and 'min-width: 0;' in who
    assert 'border-top: 1.5px solid var(--lt-text);' in _rule(
        card, '.readiness-sides'
    )
    assert 'border-bottom: 1px dotted var(--lt-muted);' in _rule(
        card, '.readiness-side'
    )
    assert '.readiness-side form {\n    flex: 1 1 100%;' in card
    assert not re.search(r'#[0-9a-fA-F]{3,8}\b', card)
    assert '--lt-brand-success,' not in card and 'ui-monospace' not in card
