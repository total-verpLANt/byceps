"""
tests.unit.services.lan_tournament.test_match_readiness_ready_since
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Pin the site readiness card's "ready since" text to the claim time.

The claim path stores naive UTC; the card shows it in the site timezone
through the real flask-babel `dateformat`/`timeformat` filters, which the
sibling rendering test stubs. Assertions match the digits only, so they do
not depend on the compiled catalogue.
"""

from datetime import datetime
from pathlib import Path
import re
from types import SimpleNamespace
from uuid import uuid4

from flask import Flask
from flask_babel import Babel
from jinja2 import Environment, FileSystemLoader, StrictUndefined
import pytest

from byceps.services.lan_tournament import lan_tournament_view_helpers
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.match_readiness import (
    ContestantIdentity,
    MatchPairing,
    side_for_contestant,
)
from byceps.services.lan_tournament.models.tournament_match import (
    MatchSide,
    TournamentMatch,
)
from byceps.services.lan_tournament.models.tournament_match_to_contestant import (
    TournamentMatchToContestant,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)


ROOT = Path(__file__).resolve().parents[4]
GENERIC = ROOT / 'byceps/services/lan_tournament/blueprints/site/templates'
PARTIAL = 'site/lan_tournament/_match_readiness.html'

OCCUPIED_SINCE = datetime(2026, 10, 15, 9, 0)
PAIRING_STARTED_AT = datetime(2026, 10, 16, 8, 0)
READY_AT_B = datetime(2026, 10, 17, 12, 5)


@pytest.fixture
def render_card(monkeypatch):
    """Return a function rendering the card with the real babel filters."""
    app = Flask(__name__)
    app.config.update(
        TESTING=True,
        SECRET_KEY='ready-since-test-only',
        BABEL_DEFAULT_LOCALE='de',
        BABEL_DEFAULT_TIMEZONE='Europe/Berlin',
    )
    Babel(app)

    env = Environment(
        loader=FileSystemLoader([GENERIC]),
        undefined=StrictUndefined,
        autoescape=True,
    )
    env.globals['_'] = lambda message, **values: (
        message % values if values else message
    )
    # Take the filters flask-babel registers on the site app's Jinja
    # environment, so the timezone conversion under test is the real one.
    for name in ('dateformat', 'timeformat'):
        env.filters[name] = app.jinja_env.filters[name]

    def render(*, ready_at_a=None, ready_at_b=None):
        match = TournamentMatch(
            id=uuid4(),
            tournament_id=uuid4(),
            group_order=None,
            match_order=1,
            round=1,
            next_match_id=None,
            confirmed_by=None,
            created_at=OCCUPIED_SINCE,
            occupied_since=OCCUPIED_SINCE,
            ready_at_a=ready_at_a,
            ready_at_b=ready_at_b,
            pairing_id=uuid4(),
            pairing_generation=3,
            readiness_revision=7,
        )
        identities = [
            ContestantIdentity(kind='participant', id=uuid4()) for _ in range(2)
        ]
        pairing = MatchPairing(
            id=match.pairing_id,
            match_id=match.id,
            tournament_id=match.tournament_id,
            generation=3,
            side_a=identities[0],
            side_b=identities[1],
            started_at=PAIRING_STARTED_AT,
        )
        contestants = [
            TournamentMatchToContestant(
                id=uuid4(),
                tournament_match_id=match.id,
                participant_id=identity.id,
                team_id=None,
                score=None,
                created_at=OCCUPIED_SINCE,
            )
            for identity in identities
        ]
        tournament = SimpleNamespace(
            id=match.tournament_id,
            party_id=uuid4(),
            name='Ready Since Cup',
            tournament_status=TournamentStatus.ONGOING,
            game_format=GameFormat.ONE_V_ONE,
            has_playoffs=False,
            contestant_type=None,
            elimination_mode=None,
        )
        monkeypatch.setattr(
            lan_tournament_view_helpers.tournament_repository,
            'get_match_pairings_for_matches',
            lambda _: {match.id: pairing},
        )
        readiness = (
            lan_tournament_view_helpers.build_match_readiness_projections(
                tournament, [match], {match.id: contestants}
            )[match.id]
        )
        assert readiness.pairing_valid
        by_side = {
            side_for_contestant(contestants, c.id, pairing=pairing): c
            for c in contestants
        }
        assert set(by_side) == set(MatchSide)
        participants = {
            identity.id: SimpleNamespace(screen_name=f'Player {index}')
            for index, identity in enumerate(identities)
        }
        with app.test_request_context():
            return env.get_template(PARTIAL).render(
                tournament=tournament,
                match=match,
                readiness=readiness,
                readiness_contestants_by_side=by_side,
                readiness_sides=set(),
                readiness_controls_enabled=False,
                teams_by_id={},
                participants_by_id=participants,
            )

    return render


def _side_html(html, side):
    """Return the markup of the `side` list item of the card."""
    return html.split(f'data-side="{side}"', 1)[1].split('</li>', 1)[0]


def _since_text(html, side):
    """Return the visible "ready since" time text of the `side` row."""
    since = re.search(
        r'<span class="readiness-side__since">.*?<time[^>]*>(.*?)</time>',
        _side_html(html, side),
        re.S,
    )
    assert since, f'side {side} shows no ready since text'
    return ' '.join(since.group(1).split())


def test_ready_since_shows_the_claim_time_in_the_site_timezone(render_card):
    html = render_card(ready_at_b=READY_AT_B)

    # The `datetime` attribute keeps the stored UTC value; the text is local.
    assert _since_text(html, 'b') == '17.10.2026, 14:05'


def test_ready_since_is_not_the_occupied_since_time(render_card):
    html = render_card(ready_at_b=READY_AT_B)

    # The card still shows the occupancy times, outside the side rows.
    assert '15.10.2026, 11:00' in html.split('<ul class="readiness-sides">')[0]

    since = _since_text(html, 'b')
    assert '15.10.2026' not in since
    assert '11:00' not in since
    assert '16.10.2026' not in since


def test_side_not_ready_has_no_ready_since(render_card):
    html = render_card(ready_at_b=READY_AT_B)

    side_a = _side_html(html, 'a')
    assert 'readiness-side__since' not in side_a
    assert '<time' not in side_a
    assert _since_text(html, 'b')
