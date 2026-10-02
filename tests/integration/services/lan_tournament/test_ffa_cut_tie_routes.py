"""
tests.integration.services.lan_tournament.test_ffa_cut_tie_routes
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Drives the FFA lobby cut tie block on the admin bracket and standings
pages and on the site bracket: from the rendered form to the advance.
"""

from datetime import datetime, UTC
from html.parser import HTMLParser
from io import BytesIO
from itertools import count
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from babel.messages.mofile import write_mo
from babel.messages.pofile import read_po
from babel.support import Translations
import flask_babel
import pytest

from byceps.database import db
from byceps.services.lan_tournament import (
    tournament_match_service,
    tournament_orga_service,
    tournament_qualification_repository,
    tournament_repository,
    tournament_seeding_repository,
    tournament_service,
)
from byceps.services.lan_tournament.models import ContestantType
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipant,
    TournamentParticipantID,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.util.uuid import generate_uuid7

from tests.helpers import http_client, log_in_user


ADMIN_URL = 'http://admin.acmecon.test/lan-tournaments'
SITE_URL = 'http://www.acmecon.test/lan-tournaments'

PRD_SENTENCE = (
    'Qualification cannot be determined automatically because of a tie.'
    ' An orga decision is required.'
)
PRD_SENTENCE_DE = (
    'Qualifikation kann aufgrund eines Gleichstands nicht automatisch'
    ' bestimmt werden. Orgaentscheidung erforderlich.'
)
TIED_TABLE = [3, 2, 1, 1]
CLEAN_TABLE = [4, 3, 2, 1]

_counter = count(1)


@pytest.fixture(scope='module')
def players(make_user):
    return [make_user(f'FfaCutTieRoutePlayer{i}') for i in range(8)]


@pytest.fixture(scope='module')
def admin(make_admin):
    user = make_admin(
        {'admin.access', 'lan_tournament.administrate', 'lan_tournament.view'}
    )
    log_in_user(user.id)
    return user


@pytest.fixture(scope='module')
def viewer(make_admin):
    user = make_admin({'admin.access', 'lan_tournament.view'})
    log_in_user(user.id)
    return user


@pytest.fixture(scope='module')
def orga(make_user):
    user = make_user('FfaCutTieRouteOrga')
    log_in_user(user.id)
    return user


@pytest.fixture(scope='module')
def bystander(make_user):
    user = make_user('FfaCutTieRouteBystander')
    log_in_user(user.id)
    return user


@pytest.fixture(scope='module')
def german_translations():
    po_path = (
        Path(__file__).parents[4]
        / 'byceps/translations/de/LC_MESSAGES/messages.po'
    )
    with po_path.open('rb') as f:
        catalog = read_po(f, locale='de')
    buffer = BytesIO()
    write_mo(buffer, catalog)
    buffer.seek(0)
    return Translations(fp=buffer)


@pytest.fixture
def german(monkeypatch, german_translations):
    monkeypatch.setattr(
        flask_babel.Domain,
        'get_translations',
        lambda self: german_translations,
    )


@pytest.fixture
def make_ffa(party, players, admin, orga):
    created = []

    def _make(*, point_table=TIED_TABLE, advancement_count=3):
        result = tournament_service.create_tournament(
            party.id,
            f'FFA Cut Tie Routes {next(_counter)}',
            contestant_type=ContestantType.SOLO,
            game_format=GameFormat.FREE_FOR_ALL,
            elimination_mode=EliminationMode.SINGLE_ELIMINATION,
            tournament_status=TournamentStatus.REGISTRATION_CLOSED,
            max_players=16,
            group_size_min=2,
            group_size_max=6,
            advancement_count=advancement_count,
            point_table=point_table,
        )
        assert result.is_ok(), result.unwrap_err()
        tournament, _ = result.unwrap()
        created.append(tournament)
        for user in players:
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
        generated = tournament_match_service.generate_ffa_round(
            tournament.id, initiator_id=admin.id
        )
        assert generated.is_ok(), generated.unwrap_err()
        started = tournament_service.change_status(
            tournament.id, TournamentStatus.ONGOING, admin.id
        )
        assert started.is_ok(), started.unwrap_err()
        tournament_orga_service.assign_orga(
            tournament.id, orga.id, orga.id
        ).unwrap()
        for match in tournament_repository.get_matches_for_round(
            tournament.id, 0
        ):
            members = [
                str(c.participant_id)
                for c in tournament_match_service.get_contestants_for_match(
                    match.id
                )
            ]
            placed = tournament_match_service.set_ffa_placements(
                match.id, {cid: i + 1 for i, cid in enumerate(members)}
            )
            assert placed.is_ok(), placed.unwrap_err()
            confirmed = tournament_match_service.confirm_ffa_match(
                match.id, admin.id
            )
            assert confirmed.is_ok(), confirmed.unwrap_err()
        return tournament

    yield _make
    db.session.rollback()
    for tournament in created:
        if tournament_repository.find_tournament(tournament.id) is not None:
            tournament_service.delete_tournament(tournament.id)


class _Forms(HTMLParser):
    """Collect what a browser without script submits for each form."""

    def __init__(self):
        super().__init__()
        self.forms = []
        self._form = None
        self._select = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'form':
            self._form = {
                'action': attrs.get('action'),
                'class': attrs.get('class', ''),
                'fields': [],
            }
            self.forms.append(self._form)
        elif self._form is None:
            return
        elif tag == 'input' and 'name' in attrs:
            self._form['fields'].append((attrs['name'], attrs.get('value', '')))
        elif tag == 'select':
            self._select = {'name': attrs.get('name'), 'selected': None}
            self._form['fields'].append(self._select)
        elif tag == 'option' and self._select is not None:
            if 'selected' in attrs:
                self._select['selected'] = attrs.get('value')

    def handle_endtag(self, tag):
        if tag == 'select':
            self._select = None
        elif tag == 'form':
            self._form = None


def _decision_forms(html, scope):
    """Return the decide forms of the page for a scope, as `(action, data)`."""
    parser = _Forms()
    parser.feed(html)
    found = []
    for form in parser.forms:
        data = {}
        order = []
        for field in form['fields']:
            if isinstance(field, dict):
                order.append(field['selected'])
            else:
                name, value = field
                if name == 'order':
                    order.append(value)
                else:
                    data[name] = value
        if data.get('scope') != scope:
            continue
        if order:
            data['order'] = order
        found.append((form['action'], data))
    return found


def _save_form(html, scope, reason='Decided at the table.'):
    forms = [
        (action, data)
        for action, data in _decision_forms(html, scope)
        if data.get('action') == 'save'
    ]
    assert len(forms) == 1, forms
    action, data = forms[0]
    return action, {**data, 'reason': reason}


def _lobby_scopes(tournament):
    return [
        tournament_match_service.ffa_lobby_scope(m)
        for m in tournament_repository.get_matches_for_round(tournament.id, 0)
    ]


def _decisions(tournament):
    return tournament_qualification_repository.get_decisions_for_tournament(
        tournament.id
    )


def _get(app, user, url):
    with http_client(app, user_id=user.id if user else None) as client:
        return client.get(url)


def _post(app, user, url, data):
    with http_client(app, user_id=user.id) as client:
        return client.post(url, data=data)


# -------------------------------------------------------------------- #
# admin


def test_admin_bracket_renders_the_blocking_lobbies(admin_app, admin, make_ffa):
    tournament = make_ffa()

    response = _get(
        admin_app, admin, f'{ADMIN_URL}/tournaments/{tournament.id}/bracket'
    )

    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert html.count('data-blocker="cut"') == 2
    for scope in _lobby_scopes(tournament):
        assert f'data-scope="{scope}"' in html
        action, data = _save_form(html, scope)
        assert action.endswith(
            f'/tournaments/{tournament.id}/qualification/decisions'
        )
        assert len(data['order']) == 2
    assert PRD_SENTENCE in html
    assert 'data-lt-order-list' in html
    assert 'data-lt-order-fallback' in html
    assert 'name="reason"' in html
    assert 'data-lt-order-root' in html
    assert 'behavior/lan_tournament_seeding.js' in html


def test_admin_bracket_renders_the_prd_sentence_in_german(
    admin_app, admin, make_ffa, german
):
    tournament = make_ffa()

    response = _get(
        admin_app, admin, f'{ADMIN_URL}/tournaments/{tournament.id}/bracket'
    )

    html = response.get_data(as_text=True)
    assert PRD_SENTENCE_DE in html
    assert PRD_SENTENCE not in html


def test_admin_ffa_standings_renders_the_blocking_lobbies(
    admin_app, admin, make_ffa
):
    tournament = make_ffa()

    response = _get(
        admin_app,
        admin,
        f'{ADMIN_URL}/tournaments/{tournament.id}/ffa_standings',
    )

    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert html.count('data-blocker="cut"') == 2
    assert PRD_SENTENCE in html


def test_admin_clean_lobbies_render_no_block(admin_app, admin, make_ffa):
    tournament = make_ffa(point_table=CLEAN_TABLE, advancement_count=2)

    for page in ('bracket', 'ffa_standings'):
        response = _get(
            admin_app, admin, f'{ADMIN_URL}/tournaments/{tournament.id}/{page}'
        )

        html = response.get_data(as_text=True)
        assert response.status_code == 200, page
        assert 'data-ffa-cut-ties' not in html, page
        assert PRD_SENTENCE not in html, page


def test_admin_view_only_user_sees_no_block(admin_app, viewer, make_ffa):
    tournament = make_ffa()

    response = _get(
        admin_app, viewer, f'{ADMIN_URL}/tournaments/{tournament.id}/bracket'
    )

    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert 'data-ffa-cut-ties' not in html
    assert 'name="reason"' not in html


def test_admin_decide_via_the_rendered_form_then_advance(
    admin_app, admin, make_ffa
):
    tournament = make_ffa()
    bracket_url = f'{ADMIN_URL}/tournaments/{tournament.id}/bracket'
    advance_url = f'{ADMIN_URL}/tournaments/{tournament.id}/advance_ffa_round'

    refused = _post(admin_app, admin, advance_url, {})
    assert refused.status_code == 302
    assert refused.headers['Location'].endswith(f'/{tournament.id}/bracket')
    db.session.rollback()
    assert (
        tournament_seeding_repository.find_seeding(tournament.id, 'ffa:SE:1')
        is None
    )

    for scope in _lobby_scopes(tournament):
        html = _get(admin_app, admin, bracket_url).get_data(as_text=True)
        action, data = _save_form(html, scope)
        response = _post(admin_app, admin, action, data)
        assert response.status_code == 302
        assert response.headers['Location'].endswith(
            f'/{tournament.id}/bracket'
        )
    db.session.rollback()
    assert set(_decisions(tournament)) == set(_lobby_scopes(tournament))

    html = _get(admin_app, admin, bracket_url).get_data(as_text=True)
    assert 'data-blocker="cut"' not in html
    assert html.count('data-decision="ffa:SE:0:') == 2
    assert 'Decided at the table.' in html

    advanced = _post(admin_app, admin, advance_url, {})
    assert advanced.status_code == 302
    location = urlparse(advanced.headers['Location'])
    assert location.path.endswith(f'/{tournament.id}/seeding')
    assert parse_qs(location.query) == {'target': ['ffa:SE:1']}


def test_admin_withdraw_via_the_rendered_form_reopens_the_tie(
    admin_app, admin, make_ffa
):
    tournament = make_ffa()
    bracket_url = f'{ADMIN_URL}/tournaments/{tournament.id}/bracket'
    scope = _lobby_scopes(tournament)[0]
    html = _get(admin_app, admin, bracket_url).get_data(as_text=True)
    action, data = _save_form(html, scope)
    _post(admin_app, admin, action, data)

    html = _get(admin_app, admin, bracket_url).get_data(as_text=True)
    withdraws = [
        form
        for form in _decision_forms(html, scope)
        if form[1].get('action') == 'withdraw'
    ]
    assert len(withdraws) == 1
    action, data = withdraws[0]
    refused = _post(admin_app, admin, action, {**data, 'reason': ''})
    assert refused.status_code == 302
    db.session.rollback()
    assert scope in _decisions(tournament)

    withdrawn = _post(
        admin_app, admin, action, {**data, 'reason': 'Counted wrong.'}
    )

    assert withdrawn.status_code == 302
    db.session.rollback()
    assert scope not in _decisions(tournament)
    html = _get(admin_app, admin, bracket_url).get_data(as_text=True)
    assert html.count('data-blocker="cut"') == 2


def test_admin_block_is_gone_once_the_next_round_is_built(
    admin_app, admin, make_ffa
):
    tournament = make_ffa()
    bracket_url = f'{ADMIN_URL}/tournaments/{tournament.id}/bracket'
    for scope in _lobby_scopes(tournament):
        html = _get(admin_app, admin, bracket_url).get_data(as_text=True)
        action, data = _save_form(html, scope)
        _post(admin_app, admin, action, data)
    from byceps.services.lan_tournament import tournament_seeding_service

    target = tournament_seeding_service.prepare_ffa_round_draft(
        tournament.id, initiator_id=admin.id
    ).unwrap()
    board = tournament_seeding_service.get_board(tournament.id, target).unwrap()
    tournament_seeding_service.generate_from_seeding(
        tournament.id,
        target,
        expected_version=board.version,
        initiator_id=admin.id,
    ).unwrap()

    html = _get(admin_app, admin, bracket_url).get_data(as_text=True)

    assert 'data-ffa-cut-ties' not in html
    scope = _lobby_scopes(tournament)[0]
    before = _decisions(tournament)[scope]
    response = _post(
        admin_app,
        admin,
        f'{ADMIN_URL}/tournaments/{tournament.id}/qualification/decisions',
        {'scope': scope, 'action': 'withdraw', 'reason': 'Too late.'},
    )
    assert response.status_code == 302
    db.session.rollback()
    assert _decisions(tournament)[scope] == before


# -------------------------------------------------------------------- #
# site


def test_site_orga_bracket_renders_the_blocking_lobbies(
    site_app, orga, make_ffa
):
    tournament = make_ffa()

    response = _get(site_app, orga, f'{SITE_URL}/{tournament.id}/bracket')

    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert html.count('data-blocker="cut"') == 2
    for scope in _lobby_scopes(tournament):
        action, data = _save_form(html, scope)
        assert action.endswith(
            f'/orga/tournaments/{tournament.id}/qualification/decisions'
        )
        assert len(data['order']) == 2
    assert PRD_SENTENCE in html
    assert 'data-lt-order-list' in html
    assert 'data-lt-order-fallback' in html
    assert 'name="reason"' in html
    assert 'behavior/lan_tournament_seeding.js' in html


def test_site_orga_bracket_renders_the_prd_sentence_in_german(
    site_app, orga, make_ffa, german
):
    tournament = make_ffa()

    response = _get(site_app, orga, f'{SITE_URL}/{tournament.id}/bracket')

    html = response.get_data(as_text=True)
    assert PRD_SENTENCE_DE in html
    assert PRD_SENTENCE not in html


def test_site_clean_lobbies_render_no_block(site_app, orga, make_ffa):
    tournament = make_ffa(point_table=CLEAN_TABLE, advancement_count=2)

    response = _get(site_app, orga, f'{SITE_URL}/{tournament.id}/bracket')

    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert 'data-ffa-cut-ties' not in html
    assert PRD_SENTENCE not in html


def test_site_participant_and_visitor_see_no_orga_data(
    site_app, bystander, orga, make_ffa
):
    tournament = make_ffa()
    scope = _lobby_scopes(tournament)[0]
    html = _get(site_app, orga, f'{SITE_URL}/{tournament.id}/bracket').get_data(
        as_text=True
    )
    action, data = _save_form(html, scope, reason='Secret orga reason.')
    _post(site_app, orga, action, data)
    url = f'{SITE_URL}/{tournament.id}/bracket'

    for user in (bystander, None):
        response = _get(site_app, user, url)

        page = response.get_data(as_text=True)
        assert response.status_code == 200
        assert 'data-ffa-cut-ties' not in page
        assert 'Secret orga reason.' not in page
        assert 'name="reason"' not in page


def test_site_non_orga_cannot_decide(site_app, bystander, orga, make_ffa):
    tournament = make_ffa()
    scope = _lobby_scopes(tournament)[0]
    html = _get(site_app, orga, f'{SITE_URL}/{tournament.id}/bracket').get_data(
        as_text=True
    )
    action, data = _save_form(html, scope)

    response = _post(site_app, bystander, action, data)

    assert response.status_code == 403
    db.session.rollback()
    assert not _decisions(tournament)


def test_site_decide_via_the_rendered_form_then_advance(
    site_app, orga, make_ffa
):
    tournament = make_ffa()
    bracket_url = f'{SITE_URL}/{tournament.id}/bracket'
    advance_url = (
        f'{SITE_URL}/orga/tournaments/{tournament.id}/advance_ffa_round'
    )
    refused = _post(site_app, orga, advance_url, {})
    assert refused.status_code == 302
    assert refused.headers['Location'].endswith(f'/{tournament.id}/bracket')

    for scope in _lobby_scopes(tournament):
        html = _get(site_app, orga, bracket_url).get_data(as_text=True)
        action, data = _save_form(html, scope)
        response = _post(site_app, orga, action, data)
        assert response.status_code == 302
        assert response.headers['Location'].endswith(
            bracket_url[len(SITE_URL) :]
        )
    db.session.rollback()
    assert set(_decisions(tournament)) == set(_lobby_scopes(tournament))

    html = _get(site_app, orga, bracket_url).get_data(as_text=True)
    assert 'data-blocker="cut"' not in html
    assert html.count('data-decision="ffa:SE:0:') == 2
    assert 'withdraw' in html

    advanced = _post(site_app, orga, advance_url, {})
    assert advanced.status_code == 302
    location = urlparse(advanced.headers['Location'])
    assert location.path.endswith(f'/orga/tournaments/{tournament.id}/seeding')
    assert parse_qs(location.query) == {'target': ['ffa:SE:1']}
