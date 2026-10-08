from dataclasses import replace
from datetime import datetime
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
import re
from typing import Any
from urllib.parse import parse_qs, urlsplit

from babel.messages.mofile import write_mo
from babel.messages.pofile import read_po
from babel.support import Translations
from flask import Blueprint, Flask
import flask_babel
from flask_babel import Babel, force_locale
from jinja2 import Environment, FileSystemLoader, StrictUndefined
import pytest

from byceps.services.lan_tournament.dashboard_view_helpers import (
    build_dashboard_context,
    format_comment_counter,
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
)
from byceps.services.lan_tournament.models.tournament_match import (
    TournamentMatchID,
)
from byceps.services.lan_tournament.tournament_dashboard_service import (
    STATUS_NOTE_BYE_ADVANCE,
    STATUS_NOTE_CORRECTED_REOPENED,
    STATUS_NOTE_EARLIER_ROUND_OPEN,
    STATUS_NOTE_LOBBY_WAITING,
    STATUS_NOTE_RESULT_CONFIRMED,
)
from byceps.services.user.models import UserID
from byceps.util.uuid import generate_uuid7


ROOT = Path(__file__).resolve().parents[4]
TEMPLATES = ROOT / 'byceps/services/lan_tournament/blueprints/common/templates'
ROWS_TEMPLATE = 'common/lan_tournament/_dashboard_rows.html'
PO = ROOT / 'byceps/translations/de/LC_MESSAGES/messages.po'

PARTY = 'pixelnacht-36'
MINUTE = 60_000_000


# ---------------------------------------------------------------- the world


@pytest.fixture(scope='module')
def app():
    app = Flask(__name__)
    app.config['TESTING'] = True
    app.config['SECRET_KEY'] = 'dashboard-rows-render-unit-test-only'
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


@pytest.fixture(scope='module')
def german_translations():
    with PO.open('rb') as f:
        catalog = read_po(f, locale='de')
    buffer = BytesIO()
    write_mo(buffer, catalog)
    buffer.seek(0)
    return Translations(fp=buffer)


@pytest.fixture
def german(monkeypatch, german_translations):
    """Read the catalogue as it stands, not the compiled one on disk."""
    monkeypatch.setattr(
        flask_babel.Domain,
        'get_translations',
        lambda self: german_translations,
    )
    with force_locale('de'):
        yield


@pytest.fixture(scope='module')
def env():
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES)),
        undefined=StrictUndefined,
        autoescape=True,
    )
    return env


# ------------------------------------------------------------- the fixtures


def at(clock: str) -> datetime:
    """Return a wall time of the party day (CEST) as the naive UTC the
    facts are stored in.
    """
    hour, minute = (int(part) for part in clock.split(':'))
    return datetime(2026, 10, 8, hour - 2, minute)


def minutes(value: float) -> int:
    return int(value * MINUTE)


def _settings() -> DashboardSettings:
    return DashboardSettings(
        yellow_minutes=15,
        red_minutes=45,
        poll_seconds=30,
        page_size=50,
        threshold_source='deployment',
    )


def _query(**fields) -> DashboardQuery:
    return DashboardQuery(per_page=50, **fields)


def _id() -> Any:
    return generate_uuid7()


def ack_summary(actor, clock, comment=None, revision=1):
    return DashboardAcknowledgementSummary(
        id=_id(),
        revision=revision,
        actor_display_name=actor,
        occurred_at=at(clock),
        comment=comment,
    )


MARA_COMMENT = 'Beide Captains kontaktiert; wir warten auf einen Spieler.'
JONAS_COMMENT = 'Spieler von Nachtbus sitzt an Platz C-14, kommt in 5 Minuten.'

GREEN, YELLOW, RED = TrafficTier.GREEN, TrafficTier.YELLOW, TrafficTier.RED

# What the service says about a row's check, by short name.
_ACK = {
    'offered': None,
    'below': AckUnavailableReason.BELOW_THRESHOLD,
    'recent': AckUnavailableReason.RECENTLY_ACKNOWLEDGED,
    'paused': AckUnavailableReason.PAUSED,
    'not_due': AckUnavailableReason.NOT_DUE,
    'unknown': AckUnavailableReason.CLOCK_UNKNOWN,
    'terminal': AckUnavailableReason.TERMINAL,
}

_WINNERS = DashboardMatchLocation(
    phase=1, bracket=Bracket.WINNERS, round=1, match_order=2
)
_LOSERS = DashboardMatchLocation(
    phase=1, bracket=Bracket.LOSERS, round=1, match_order=0
)
_GROUP_A = DashboardMatchLocation(
    phase=1, group_order=0, round=0, match_order=1
)


def _row(defaults: dict[str, Any], fields: dict[str, Any]) -> DashboardRow:
    values = {**defaults, **fields}
    values['ack_unavailable_reason'] = _ACK[values.pop('ack')]
    if values['state'] is DashboardRowState.DUE:
        values.setdefault('episode_id', _id())
    return DashboardRow(**values)


def kupfer(**fields) -> DashboardRow:
    """The mockup's `KC`: a due knockout match, one side ready, pinned."""
    return _row(
        dict(
            match_id=TournamentMatchID(_id()),
            tournament_id=TournamentID(_id()),
            tournament_name='Kupfer-Cup',
            game='Arena Five',
            game_format=GameFormat.ONE_V_ONE,
            elimination_mode=EliminationMode.SINGLE_ELIMINATION,
            location=_WINNERS,
            contestant_names=('Kupferfüchse', 'Nachtbus'),
            orga_names=('Mara', 'Jonas'),
            state=DashboardRowState.DUE,
            created_at=at('13:10'),
            occupied_since=at('13:40'),
            episode_opened_at=at('14:00'),
            last_changed_at=at('14:03'),
            readiness_available=True,
            ready_at_a=at('14:03'),
            review_available=True,
            pinned_at=at('14:10'),
            pinned_by_name='Alex',
            ack='below',
        ),
        fields,
    )


def neon(**fields) -> DashboardRow:
    """The mockup's `NE`: a due group match at yellow, nobody ready."""
    return _row(
        dict(
            match_id=TournamentMatchID(_id()),
            tournament_id=TournamentID(_id()),
            tournament_name='Neon-Duell',
            game='Vector Duel',
            game_format=GameFormat.ONE_V_ONE,
            elimination_mode=EliminationMode.ROUND_ROBIN,
            location=_GROUP_A,
            contestant_names=('Nori', 'Komet'),
            orga_names=('Jonas',),
            state=DashboardRowState.DUE,
            tier=YELLOW,
            created_at=at('13:30'),
            occupied_since=at('13:30'),
            episode_opened_at=at('14:20'),
            last_changed_at=at('14:20'),
            total_active_wait_us=minutes(30),
            alert_interval_us=minutes(30),
            readiness_available=True,
            review_available=True,
            ack='offered',
        ),
        fields,
    )


def orbit(**fields) -> DashboardRow:
    """The mockup's `OR`: a complete lobby at green, review not installed."""
    return _row(
        dict(
            match_id=TournamentMatchID(_id()),
            tournament_id=TournamentID(_id()),
            tournament_name='Orbit-Lobby',
            game='Orbit Rally',
            game_format=GameFormat.FREE_FOR_ALL,
            elimination_mode=EliminationMode.SINGLE_ELIMINATION,
            location=DashboardMatchLocation(
                phase=1, bracket=Bracket.WINNERS, round=0, match_order=0
            ),
            contestant_names=('Tess', 'Echo', 'Lio', 'Vale'),
            orga_names=('Alex',),
            state=DashboardRowState.DUE,
            tier=GREEN,
            created_at=at('14:30'),
            occupied_since=at('14:38'),
            episode_opened_at=at('14:41'),
            last_changed_at=at('14:41'),
            total_active_wait_us=minutes(9),
            alert_interval_us=minutes(9),
            review_available=False,
            ack='below',
        ),
        fields,
    )


def timed(factory, tier, total, alert, **fields) -> DashboardRow:
    """A due row at `tier`, with its total wait and alert interval (min)."""
    return factory(
        tier=tier,
        total_active_wait_us=minutes(total),
        alert_interval_us=minutes(alert),
        **fields,
    )


def conflict_of_kupfer() -> DashboardConflict:
    """Nori, needed through Kupferfüchse, and again in the Neon-Duell."""
    return DashboardConflict(
        user_id=UserID(_id()),
        user_display_name='Nori',
        via_team_name='Kupferfüchse',
        visible_refs=(
            DashboardConflictRef(
                match_id=TournamentMatchID(_id()),
                tournament_id=TournamentID(_id()),
                tournament_name='Neon-Duell',
                location=_GROUP_A,
                contestant_names=('Nori', 'Komet'),
                game_format=GameFormat.ONE_V_ONE,
            ),
        ),
    )


def conflict_of_neon() -> DashboardConflict:
    """Nori, needed in the Neon-Duell and again as a Kupferfüchse member."""
    return DashboardConflict(
        user_id=UserID(_id()),
        user_display_name='Nori',
        visible_refs=(
            DashboardConflictRef(
                match_id=TournamentMatchID(_id()),
                tournament_id=TournamentID(_id()),
                tournament_name='Kupfer-Cup',
                location=_WINNERS,
                contestant_names=('Kupferfüchse', 'Nachtbus'),
                via_team_name='Kupferfüchse',
                game_format=GameFormat.ONE_V_ONE,
            ),
        ),
    )


def external_conflict(name='Nori') -> DashboardConflict:
    return DashboardConflict(
        user_id=UserID(_id()),
        user_display_name=name,
        has_external_conflict=True,
    )


def kupfer_50(**fields) -> DashboardRow:
    """The mockup's `KC50`: yellow again after Mara's check at 14:15."""
    mara = ack_summary('Mara', '14:15', MARA_COMMENT)
    return timed(
        kupfer,
        YELLOW,
        30,
        15,
        **{
            **dict(
                latest_acknowledgement=mara,
                recent_acknowledgements=(mara,),
                acknowledgement_count=1,
                ack_revision=1,
                conflicts=(conflict_of_kupfer(),),
                ack='offered',
            ),
            **fields,
        },
    )


def neon_50(**fields) -> DashboardRow:
    return neon(**{**dict(conflicts=(conflict_of_neon(),)), **fields})


def orbit_50(**fields) -> DashboardRow:
    return orbit(**{**dict(orga_names=('Alex', 'Jonas')), **fields})


def _page(rows, *, as_of) -> DashboardPage:
    return DashboardPage(
        rows=tuple(rows),
        as_of=as_of,
        total_count=len(rows),
        page=1,
        per_page=50,
        total_pages=1 if rows else 0,
        tier_counts=DashboardTierCounts(),
        non_actionable_counts=DashboardNonActionableCounts(),
    )


def context_of(
    rows, *, clock='14:50', surface='admin', token='token-1', query=None
):
    return build_dashboard_context(
        _page(rows, as_of=at(clock)),
        query or _query(),
        _settings(),
        surface=surface,
        csrf_token=token,
        party_id=PARTY if surface == 'admin' else None,
    )


def render(env, context, action=None) -> str:
    template = env.get_template(ROWS_TEMPLATE)
    return str(template.module.render_rows(context, 'count-heading', action))


LONG_TEAM = 'Die außerordentlich unpünktlichen Kupferfüchse aus dem Nordflügel'
LONG_ORGA = 'Maximiliane Theodora Hinterkaifeck-Sonnleitner'
LONG_COMMENT = (
    'Wir haben beide Captains am Turniertresen und per Durchsage erreicht.'
    ' Bei den Kupferfüchsen fehlt ein Spieler, der laut Teamkollegen noch im'
    ' Catering-Bereich steht; Nachtbus ist vollständig, wartet aber auf ein'
    ' Headset-Ersatzteil vom Technikstand. Beide Teams sind einverstanden,'
    ' bis 15:05 zu warten. Danach sprechen wir mit der Turnierleitung über'
    ' eine Wertung. Bitte bei Nachfragen nicht erneut beide Teams ausrufen,'
    ' die Durchsage lief bereits zweimal. Stand der Technik: Headset kommt'
    ' in etwa zehn Minuten.'
)[:500]


def frame_panels() -> dict[str, list[tuple[str, list[DashboardRow]]]]:
    """The rows of the ten design frames: one (clock, rows) per panel."""
    mara = ack_summary('Mara', '14:15', MARA_COMMENT)
    jonas = ack_summary('Jonas', '14:50', JONAS_COMMENT, revision=2)
    frames: dict[str, list[tuple[str, list[DashboardRow]]]] = {}

    # S2a: the tier boundaries; total wait is not the alert interval.
    frames['S2a'] = [
        ('14:15', [timed(kupfer, GREEN, 14.98, 14.98)]),
        ('14:15', [timed(kupfer, YELLOW, 15, 15, ack='offered')]),
        ('14:45', [timed(kupfer, RED, 45, 45, ack='offered')]),
        ('14:50', [kupfer_50(conflicts=())]),
    ]

    # S2b: nobody, one side, both sides ready, and a lobby.
    frames['S2b'] = [
        (
            '14:50',
            [
                neon(),
                timed(kupfer, YELLOW, 15, 15, pinned_at=None, ack='offered'),
                timed(
                    kupfer,
                    GREEN,
                    12,
                    12,
                    pinned_at=None,
                    ready_at_b=at('14:11'),
                    last_changed_at=at('14:11'),
                ),
                orbit_50(),
            ],
        )
    ]

    # S3c: the check is confirmed by the server; alert interval back to 0.
    frames['S3c'] = [
        (
            '14:15',
            [
                timed(
                    kupfer,
                    GREEN,
                    15,
                    0,
                    latest_acknowledgement=mara,
                    recent_acknowledgements=(mara,),
                    acknowledgement_count=1,
                    ack_revision=1,
                    ack='recent',
                )
            ],
        )
    ]

    # S3g: yellow again; Mara's check stays next to the renewed alert.
    frames['S3g'] = [('14:50', [kupfer_50(conflicts=())])]

    # S3k: Jonas checked at 14:50; the history lists both checks.
    frames['S3k'] = [
        (
            '14:51',
            [
                timed(
                    kupfer,
                    GREEN,
                    31,
                    1,
                    latest_acknowledgement=jonas,
                    recent_acknowledgements=(jonas, mara),
                    acknowledgement_count=2,
                    ack_revision=2,
                    ack='recent',
                )
            ],
        )
    ]

    # S4a: pin, check, authorized conflict and an open review together.
    frames['S4a'] = [
        (
            '14:50',
            [
                kupfer_50(review_open=True),
                neon_50(review_open=True),
            ],
        )
    ]

    # S4c: long names and a 500 character comment; a deleted orga.
    long_ack = ack_summary(LONG_ORGA, '14:15', LONG_COMMENT)
    deleted_ack = ack_summary('Gelöschte Orga', '14:02', None, revision=0)
    long_conflict = replace(conflict_of_kupfer(), via_team_name=LONG_TEAM)
    frames['S4c'] = [
        (
            '14:50',
            [
                kupfer_50(
                    contestant_names=(LONG_TEAM, 'Nachtbus Linie N7 Ersatzverkehr'),
                    orga_names=(LONG_ORGA, 'Jonas'),
                    conflicts=(long_conflict,),
                    latest_acknowledgement=long_ack,
                    recent_acknowledgements=(long_ack, deleted_ack),
                    acknowledgement_count=2,
                )
            ],
        )
    ]  # fmt: skip

    # S5a: upcoming, partial, bye and a lobby that waits.
    frames['S5a'] = [
        (
            '14:50',
            [
                neon(
                    state=DashboardRowState.UPCOMING,
                    tier=None,
                    total_active_wait_us=None,
                    alert_interval_us=None,
                    episode_opened_at=None,
                    created_at=at('14:20'),
                    occupied_since=at('14:20'),
                    last_changed_at=at('14:20'),
                    contestant_names=('Nori', 'Lumen'),
                    location=DashboardMatchLocation(
                        phase=1, group_order=0, round=1, match_order=0
                    ),
                    status_note=DashboardStatusNote(
                        code=STATUS_NOTE_EARLIER_ROUND_OPEN,
                        params=(('round', 0),),
                    ),
                    ack='not_due',
                ),
                kupfer(
                    state=DashboardRowState.PARTIAL,
                    location=_LOSERS,
                    contestant_names=('Nachtbus',),
                    pinned_at=None,
                    pinned_by_name=None,
                    episode_opened_at=None,
                    occupied_since=None,
                    ready_at_a=None,
                    last_changed_at=at('14:05'),
                    ack='not_due',
                ),
                kupfer(
                    state=DashboardRowState.BYE,
                    location=DashboardMatchLocation(
                        phase=1, bracket=Bracket.LOSERS, round=0, match_order=1
                    ),
                    contestant_names=('Funkloch',),
                    pinned_at=None,
                    pinned_by_name=None,
                    episode_opened_at=None,
                    occupied_since=at('13:10'),
                    ready_at_a=None,
                    last_changed_at=at('13:10'),
                    status_note=DashboardStatusNote(code=STATUS_NOTE_BYE_ADVANCE),
                    ack='terminal',
                ),
                orbit(
                    state=DashboardRowState.AWAITING_LOBBY,
                    tier=None,
                    total_active_wait_us=None,
                    alert_interval_us=None,
                    contestant_names=('Tess', 'Lio'),
                    location=DashboardMatchLocation(
                        phase=1, bracket=Bracket.WINNERS, round=1, match_order=0
                    ),
                    episode_opened_at=None,
                    occupied_since=None,
                    created_at=at('14:41'),
                    last_changed_at=at('14:41'),
                    review_available=True,
                    status_note=DashboardStatusNote(
                        code=STATUS_NOTE_LOBBY_WAITING,
                        params=(('filled', 2), ('size', 4), ('after_round', 0)),
                    ),
                    ack='not_due',
                ),
            ],
        )
    ]  # fmt: skip

    # S5c: a complete lobby, a confirmed match and a legacy match.
    frames['S5c'] = [
        (
            '14:50',
            [
                orbit_50(),
                kupfer(
                    state=DashboardRowState.DONE,
                    location=DashboardMatchLocation(
                        phase=1, bracket=Bracket.WINNERS, round=0, match_order=0
                    ),
                    contestant_names=('Kupferfüchse', 'Lötzinn'),
                    ready_at_b=at('13:24'),
                    ready_at_a=at('13:21'),
                    pinned_at=at('13:20'),
                    closed_episode_wait_us=minutes(14),
                    episode_opened_at=at('13:20'),
                    occupied_since=at('13:15'),
                    last_changed_at=at('13:34'),
                    status_note=DashboardStatusNote(
                        code=STATUS_NOTE_RESULT_CONFIRMED,
                        params=(('score_a', 2), ('score_b', 1)),
                    ),
                    ack='terminal',
                ),
                kupfer(
                    state=DashboardRowState.UNKNOWN,
                    location=DashboardMatchLocation(
                        phase=1, round=0, match_order=0
                    ),
                    contestant_names=('Altmeister', 'Nachtbus'),
                    pinned_at=None,
                    pinned_by_name=None,
                    ready_at_a=None,
                    readiness_available=False,
                    episode_opened_at=None,
                    occupied_since=None,
                    last_changed_at=None,
                    ack='unknown',
                ),
            ],
        )
    ]  # fmt: skip

    # S5d: a corrected result opens a new episode; no inherited check.
    frames['S5d'] = [
        (
            '15:35',
            [
                timed(
                    kupfer,
                    GREEN,
                    3,
                    3,
                    episode_opened_at=at('15:32'),
                    last_changed_at=at('15:32'),
                    has_prior_episode=True,
                    status_note=DashboardStatusNote(
                        code=STATUS_NOTE_CORRECTED_REOPENED
                    ),
                )
            ],
        )
    ]

    return frames


# -------------------------------------------------------------- the markup

_VOID = frozenset({'input', 'br', 'hr', 'img', 'meta', 'link'})


def _has(node, name, value) -> bool:
    """Tell whether an attribute is there (`True`) or has this value."""
    if name not in node.attrs:
        return False
    return value is True or node.attrs[name] == value


class Node:
    def __init__(self, tag, attrs, parent=None):
        self.tag = tag
        self.attrs = dict(attrs)
        self.parent = parent
        self.children: list[Node | str] = []

    @property
    def classes(self) -> list[str]:
        return (self.attrs.get('class') or '').split()

    def walk(self):
        for child in self.children:
            if isinstance(child, Node):
                yield child
                yield from child.walk()

    def find_all(self, tag=None, cls=None, **attrs) -> list['Node']:
        found = []
        for node in self.walk():
            if tag is not None and node.tag != tag:
                continue
            if cls is not None and cls not in node.classes:
                continue
            if any(not _has(node, k, v) for k, v in attrs.items()):
                continue
            found.append(node)
        return found

    def find(self, tag=None, cls=None, **attrs) -> 'Node | None':
        found = self.find_all(tag, cls, **attrs)
        return found[0] if found else None

    def one(self, tag=None, cls=None, **attrs) -> 'Node':
        """Return the first match; a row that lacks it fails here."""
        node = self.find(tag, cls, **attrs)
        assert node is not None, (tag, cls, attrs)
        return node

    @property
    def text(self) -> str:
        parts: list[str] = []
        for child in self.children:
            if isinstance(child, str):
                parts.append(child)
            elif child.tag == 'q':
                parts.append(f'„{child.text}“')
            else:
                parts.append(child.text)
        return re.sub(r'\s+', ' ', ''.join(parts)).strip()

    @property
    def strings(self) -> list[str]:
        """The text pieces in reading order, a quotation as one piece."""
        pieces: list[str] = []
        for child in self.children:
            if isinstance(child, str):
                if child.strip():
                    pieces.append(re.sub(r'\s+', ' ', child).strip())
            elif child.tag == 'q':
                pieces.append(f'„{child.text}“')
            else:
                pieces.extend(child.strings)
        return pieces

    @property
    def raw_text(self) -> str:
        """The text as the browser reads it: no whitespace is collapsed."""
        return ''.join(
            child if isinstance(child, str) else child.raw_text
            for child in self.children
        )


class _Builder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node('#root', {})
        self.current = self.root

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs, self.current)
        self.current.children.append(node)
        if tag not in _VOID:
            self.current = node

    def handle_endtag(self, tag):
        if tag in _VOID:
            return
        node = self.current
        while node is not self.root and node.tag != tag:
            node = node.parent
        self.current = node.parent if node is not self.root else self.root

    def handle_data(self, data):
        self.current.children.append(data)


def parse(html: str) -> Node:
    builder = _Builder()
    builder.feed(html)
    return builder.root


def rows_of(html: str, prefix='ltd-') -> list[Node]:
    return parse(html).find_all('li', prefix + 'row')


def _texts(nodes) -> list[str]:
    return [node.text for node in nodes]


def facts(li: Node, prefix='ltd-') -> dict[str, Any]:
    """Read what a row says, as a viewer reads it."""

    def text_of(tag, cls) -> str | None:
        node = li.find(tag, prefix + cls)
        return node.text if node is not None else None

    tier = li.one('div', prefix + 'tier')
    ready = li.find('p', prefix + 'ready')
    act = li.one('div', prefix + 'act')
    opener = li.find('button', **{'data-ack-open': True})
    reason = act.find('p', prefix + 'why')
    pin = li.find('button', **{'data-pin': True})
    record = li.find('div', prefix + 'rec')
    history = li.find('ol', prefix + 'hist')
    disclosure = li.find('button', prefix + 'disc')
    lobby = li.find('ul', prefix + 'lobby')
    return {
        'tier': tier.strings,
        'tier_class': [c for c in tier.classes if c != prefix + 'tier'],
        'context': li.one('p', prefix + 'ctx').strings,
        'title': li.one('h3').text,
        'lobby': _texts(lobby.find_all('li')) if lobby is not None else None,
        'location': text_of('p', 'loc'),
        'orgas': text_of('p', 'orgas'),
        'ready': ready.strings if ready is not None else None,
        'signals': [n.strings for n in li.find_all('span', prefix + 'sig')],
        'conflicts': [n.strings for n in li.find_all('div', prefix + 'conf')],
        'note': text_of('p', 'note'),
        'times': [
            [row.one('dt').text, row.one('dd').text]
            for row in li.one('dl', prefix + 'times').find_all('div')
        ],
        'opener': opener.text if opener is not None else None,
        'reason': reason.text if reason is not None else None,
        'pin': pin.text if pin is not None else None,
        'links': _texts(act.one('span', prefix + 'links').find_all('a')),
        'record': record.one('p', prefix + 'rech').strings
        if record is not None
        else None,
        'record_comment': text_of('p', 'recc'),
        'disclosure': disclosure.text if disclosure is not None else None,
        'history': [n.strings for n in history.find_all('li')]
        if history is not None
        else None,
        'form': li.find('form', prefix + 'form') is not None,
    }


# ------------------------------------------------------------ the frames


def _times(wait, created, *, occupied='13:40', due='14:00', changed='14:03'):
    return [
        ['Aktive Wartezeit gesamt', wait],
        ['Besetzt seit', occupied],
        ['Fällig seit (diese Episode)', due],
        ['Letzte Match-Änderung', changed],
        ['Erstellt', created],
    ]


_KC_READY = [
    'Bereitschaft',
    'Eine Seite bereit',
    '✓',
    'Kupferfüchse bereit seit 14:03',
    '○',
    'Nachtbus nicht bereit',
]
_MARA = ['Zuletzt geprüft von Mara um 14:15']
_MARA_FOLLOW = [*_MARA, '▲', 'Erneute Verzögerung – nochmals prüfen']
_MARA_QUOTE = '„Beide Captains kontaktiert; wir warten auf einen Spieler.“'
_JONAS_QUOTE = '„Spieler von Nachtbus sitzt an Platz C-14, kommt in 5 Minuten.“'
_BELOW = 'Prüfen ab 15 min Alarmintervall.'

# What each design frame shows, as the snapshot spells it. Only the bracket
# and pool words follow the module's catalogue (Gewinnerrunde, Gewinner-Pool)
# and a few row notes are worded by the service; see the handoff.
# fmt: off
EXPECTED: dict[str, list[dict[str, Any]]] = {
    'S2a': [
        dict(tier=['○', 'Unter Warnschwelle', '14 min', 'Alarmintervall'],
             tier_class=['t-g'], opener=None, reason=_BELOW, record=None,
             times=_times('14 min', '13:10 · Alter 1 h 05 min')),
        dict(tier=['▲', 'Verzögerung prüfen', '15 min', 'Alarmintervall'],
             tier_class=['t-y'], opener='Prüfung erfassen…', reason=None,
             times=_times('15 min', '13:10 · Alter 1 h 05 min')),
        dict(tier=['■', 'Lange Verzögerung', '45 min', 'Alarmintervall'],
             tier_class=['t-r'], opener='Prüfung erfassen…', reason=None,
             times=_times('45 min', '13:10 · Alter 1 h 35 min')),
        dict(tier=['▲', 'Verzögerung prüfen', '15 min', 'Alarmintervall'],
             tier_class=['t-y'], opener='Nochmals prüfen', reason=None,
             record=_MARA_FOLLOW, record_comment=_MARA_QUOTE,
             times=_times('30 min', '13:10 · Alter 1 h 40 min')),
    ],
    'S2b': [
        dict(title='Nori gegen Komet', context=['Neon-Duell', 'Vector Duel', 'Jeder gegen jeden'],
             ready=['Bereitschaft', 'Niemand bereit', '○', 'Nori nicht bereit', '○', 'Komet nicht bereit']),
        dict(ready=_KC_READY, tier_class=['t-y']),
        dict(ready=['Bereitschaft', 'Beide bereit', '✓', 'Kupferfüchse bereit seit 14:03', '✓', 'Nachtbus bereit seit 14:11'],
             tier=['○', 'Unter Warnschwelle', '12 min', 'Alarmintervall'], tier_class=['t-g']),
        dict(title='Lobby 1 · 4 Teilnehmende', lobby=['Tess', 'Echo', 'Lio', 'Vale'],
             context=['Orbit-Lobby', 'Orbit Rally', 'Free-for-all'],
             ready=['Bereitschaft', 'Nicht verfügbar (Lobby-Format)'],
             signals=[['Überprüfung: nicht verfügbar']], orgas='Zuständig Alex, Jonas'),
    ],
    'S3c': [
        dict(tier=['○', 'Unter Warnschwelle', 'unter 1 min', 'Alarmintervall'],
             tier_class=['t-g'], opener=None,
             reason='Gerade geprüft. Erneut ab 15 min Alarmintervall.',
             record=_MARA, record_comment=_MARA_QUOTE, disclosure=None,
             times=_times('15 min', '13:10 · Alter 1 h 05 min')),
    ],
    'S3g': [
        dict(tier=['▲', 'Verzögerung prüfen', '15 min', 'Alarmintervall'],
             tier_class=['t-y'], opener='Nochmals prüfen', reason=None,
             record=_MARA_FOLLOW, record_comment=_MARA_QUOTE,
             times=_times('30 min', '13:10 · Alter 1 h 40 min')),
    ],
    'S3k': [
        dict(tier=['○', 'Unter Warnschwelle', '1 min', 'Alarmintervall'],
             opener=None, reason='Gerade geprüft. Erneut ab 15 min Alarmintervall.',
             record=['Zuletzt geprüft von Jonas um 14:50'], record_comment=_JONAS_QUOTE,
             disclosure='Letzte Prüfungen anzeigen (2 in dieser Episode)',
             history=[['Jonas', '· 14:50 · aktuell', _JONAS_QUOTE],
                      ['Mara', '· 14:15', _MARA_QUOTE]]),
    ],
    'S4a': [
        dict(signals=[['◆', 'Angepinnt von Alex um 14:10'], ['⇄', 'Konflikt'], ['⚑', 'Überprüfung offen']],
             conflicts=[['⇄', 'Nori', '(Mitglied von Kupferfüchse) ist gleichzeitig in einer anderen Begegnung benötigt:',
                         'Neon-Duell · Gruppe A · Runde 1 · Spiel 2: Nori gegen Komet', 'Zur Begegnung']],
             record=_MARA_FOLLOW),
        dict(signals=[['⇄', 'Konflikt'], ['⚑', 'Überprüfung offen']],
             conflicts=[['⇄', 'Nori', 'ist gleichzeitig in einer anderen Begegnung benötigt:',
                         'Kupfer-Cup · Gewinnerrunde · Runde 2 · Spiel 3: Kupferfüchse gegen Nachtbus – als Mitglied von Kupferfüchse',
                         'Zur Begegnung']],
             record=None, opener='Prüfung erfassen…', pin='Für das Orga-Team anpinnen'),
    ],
    'S4c': [
        dict(title=f'{LONG_TEAM} gegen Nachtbus Linie N7 Ersatzverkehr',
             orgas=f'Zuständig {LONG_ORGA}, Jonas',
             ready=['Bereitschaft', 'Eine Seite bereit', '✓', f'{LONG_TEAM} bereit seit 14:03',
                    '○', 'Nachtbus Linie N7 Ersatzverkehr nicht bereit'],
             conflicts=[['⇄', 'Nori', f'(Mitglied von {LONG_TEAM}) ist gleichzeitig in einer anderen Begegnung benötigt:',
                         'Neon-Duell · Gruppe A · Runde 1 · Spiel 2: Nori gegen Komet', 'Zur Begegnung']],
             record=[f'Zuletzt geprüft von {LONG_ORGA} um 14:15', '▲', 'Erneute Verzögerung – nochmals prüfen'],
             record_comment=f'„{LONG_COMMENT}“',
             disclosure='Letzte Prüfungen anzeigen (2 in dieser Episode)',
             history=[[LONG_ORGA, '· 14:15 · aktuell', f'„{LONG_COMMENT}“'],
                      ['Gelöschte Orga', '· 14:02', 'ohne Kommentar']]),
    ],
    'S5a': [
        dict(tier=['›', 'Kommend', 'Noch nicht fällig · keine Stufe'], tier_class=['t-upcoming'],
             title='Nori gegen Lumen', location='Gruppe A · Runde 2 · Spiel 1',
             ready=['Bereitschaft', 'Niemand bereit', '○', 'Nori nicht bereit', '○', 'Lumen nicht bereit'],
             note='Runde 1 der Gruppe A ist noch offen. Künftige Begegnung, daher kein Konflikt.',
             times=[['Aktive Wartezeit gesamt', 'Noch nicht fällig'], ['Besetzt seit', '14:20'],
                    ['Letzte Match-Änderung', '14:20'], ['Erstellt', '14:20 · Alter 30 min']],
             opener=None, reason='Nicht fällig – kein Prüfen.', pin='Für das Orga-Team anpinnen'),
        dict(tier=['◌', 'Unvollständig', 'Gegner offen · nicht spielbar'], tier_class=['t-partial'],
             title='Nachtbus gegen Gegner noch offen', ready=None, note=None,
             times=[['Aktive Wartezeit gesamt', 'Noch nicht fällig'], ['Besetzt seit', 'Noch nicht besetzt'],
                    ['Letzte Match-Änderung', '14:05'], ['Erstellt', '13:10 · Alter 1 h 40 min']],
             reason='Nicht fällig – kein Prüfen.', pin='Für das Orga-Team anpinnen'),
        dict(tier=['»', 'Freilos', 'Kein Spiel nötig'], tier_class=['t-bye'],
             title='Funkloch gegen kein Gegner (Freilos)', ready=None,
             note='Funkloch rückt ohne Spiel weiter.', reason='Abgeschlossen – keine Aktionen.', pin=None),
        dict(tier=['…', 'Wartet auf Lobby', 'Lobby noch nicht vollständig'], tier_class=['t-wait'],
             title='Lobby 1 · 2 Teilnehmende', lobby=['Tess', 'Lio'],
             ready=['Bereitschaft', 'Nicht verfügbar (Lobby-Format)'],
             note='2 von 4 Plätzen besetzt. Die Lobby wird erst nach Runde 1 vollständig erzeugt.',
             times=[['Aktive Wartezeit gesamt', 'Noch nicht fällig'], ['Besetzt seit', 'Noch nicht besetzt'],
                    ['Letzte Match-Änderung', '14:41'], ['Erstellt', '14:41 · Alter 9 min']],
             reason='Nicht fällig – kein Prüfen.', signals=[]),
    ],
    'S5c': [
        dict(title='Lobby 1 · 4 Teilnehmende', tier_class=['t-g'], ready=['Bereitschaft', 'Nicht verfügbar (Lobby-Format)']),
        dict(tier=['—', 'Abgeschlossen', 'Bestätigt · keine Aktionen'], tier_class=['t-done'],
             signals=[['◆', 'Angepinnt von Alex um 13:20']],
             note='Ergebnis 2:1 bestätigt um 13:34.',
             times=[['Aktive Wartezeit gesamt', '14 min (bis Bestätigung)'], ['Besetzt seit', '13:15'],
                    ['Fällig seit (diese Episode)', '13:20'], ['Letzte Match-Änderung', '13:34'],
                    ['Erstellt', '13:10 · Alter 1 h 40 min']],
             opener=None, reason='Abgeschlossen – keine Aktionen.', pin=None,
             links=['Zur Begegnung', 'Zum Turnier']),
        dict(tier=['?', 'Zeit unbekannt', 'Keine Stufe ohne Zeitbasis'], tier_class=['t-unknown'],
             ready=['Bereitschaft', 'Nicht verfügbar'],
             times=[['Aktive Wartezeit gesamt', 'Nicht verfügbar'],
                    ['Besetzt seit', 'Historischer Zeitpunkt unbekannt'],
                    ['Letzte Match-Änderung', 'Historischer Zeitpunkt unbekannt'],
                    ['Erstellt', '13:10 · Alter 1 h 40 min']],
             opener=None, reason='Ohne Zeitbasis kein Prüfen.'),
    ],
    'S5d': [
        dict(tier=['○', 'Unter Warnschwelle', '3 min', 'Alarmintervall'],
             signals=[['◆', 'Angepinnt von Alex um 14:10'], ['↻', 'Neue Episode seit 15:32']],
             note='Ergebnis wurde um 15:32 korrigiert und wieder geöffnet. Frühere Prüfungen gehören zur vorherigen Episode und gelten hier nicht.',
             times=_times('3 min', '13:10 · Alter 2 h 25 min', due='15:32', changed='15:32'),
             record=None, record_comment=None, disclosure=None, history=None),
    ],
}
# fmt: on


def _render_frame(env, frame, surface='admin') -> list[Node]:
    rows: list[Node] = []
    for clock, panel_rows in frame_panels()[frame]:
        context = context_of(panel_rows, clock=clock, surface=surface)
        rows += rows_of(render(env, context))
    return rows


@pytest.mark.parametrize('frame', list(EXPECTED))
def test_frame_rows_match_the_snapshot_copy(env, german, frame):
    rows = _render_frame(env, frame)

    assert len(rows) == len(EXPECTED[frame])
    for index, (row, expected) in enumerate(
        zip(rows, EXPECTED[frame], strict=True)
    ):
        actual = facts(row)
        for key, value in expected.items():
            assert actual[key] == value, (frame, index, key)


def test_every_frame_of_the_acceptance_list_is_covered():
    assert set(EXPECTED) == {
        'S2a', 'S2b', 'S3c', 'S3g', 'S3k', 'S4a', 'S4c', 'S5a', 'S5c', 'S5d',
    }  # fmt: skip


# --------------------------------------------------------- the row states


def paused_row(**fields) -> DashboardRow:
    """A paused match: the clock stands still, no tier and no check."""
    return kupfer(
        **{
            **dict(
                state=DashboardRowState.PAUSED,
                total_active_wait_us=minutes(20),
                alert_interval_us=minutes(5),
                ack='paused',
            ),
            **fields,
        }
    )


def partial_row(**fields) -> DashboardRow:
    return kupfer(
        **{
            **dict(
                state=DashboardRowState.PARTIAL,
                location=_LOSERS,
                contestant_names=('Nachtbus',),
                pinned_at=None,
                pinned_by_name=None,
                episode_opened_at=None,
                occupied_since=None,
                ready_at_a=None,
                ack='not_due',
            ),
            **fields,
        }
    )


def legacy_row(**fields) -> DashboardRow:
    """A match from before the clock: every fact is unknown."""
    return kupfer(
        **{
            **dict(
                state=DashboardRowState.UNKNOWN,
                pinned_at=None,
                pinned_by_name=None,
                ready_at_a=None,
                readiness_available=False,
                episode_opened_at=None,
                occupied_since=None,
                last_changed_at=None,
                ack='unknown',
            ),
            **fields,
        }
    )


def _every_state_rows() -> list[tuple[str, DashboardRow]]:
    mara = ack_summary('Mara', '14:15', MARA_COMMENT)
    history = tuple(
        ack_summary(actor, clock, comment, revision=index + 1)
        for index, (actor, clock, comment) in enumerate(
            [
                ('Jonas', '14:50', None),
                ('Mara', '14:30', 'x'),
                ('Alex', '14:15', None),
            ]
        )
    )
    return [
        ('green', timed(kupfer, GREEN, 5, 5)),
        ('green after a check', timed(
            kupfer, GREEN, 20, 1, latest_acknowledgement=mara,
            recent_acknowledgements=(mara,), acknowledgement_count=1,
            ack='recent')),
        ('yellow', timed(kupfer, YELLOW, 15, 15, ack='offered')),
        ('red', timed(kupfer, RED, 60, 45, ack='offered')),
        ('red after a check', replace(
            kupfer_50(), tier=RED, total_active_wait_us=minutes(60),
            alert_interval_us=minutes(45))),
        ('three checks', timed(
            kupfer, YELLOW, 70, 20, latest_acknowledgement=history[0],
            recent_acknowledgements=history, acknowledgement_count=5,
            ack='offered')),
        ('paused', paused_row()),
        ('paused after a check', paused_row(
            latest_acknowledgement=mara, recent_acknowledgements=(mara,))),
        ('upcoming', neon(
            state=DashboardRowState.UPCOMING, tier=None,
            total_active_wait_us=None, alert_interval_us=None,
            episode_opened_at=None, ack='not_due')),
        ('partial', partial_row()),
        ('bye', kupfer(
            state=DashboardRowState.BYE, contestant_names=('Funkloch',),
            ack='terminal')),
        ('awaiting lobby', orbit(
            state=DashboardRowState.AWAITING_LOBBY, tier=None,
            total_active_wait_us=None, alert_interval_us=None,
            contestant_names=('Tess', 'Lio'), episode_opened_at=None,
            occupied_since=None, ack='not_due')),
        ('done', kupfer(
            state=DashboardRowState.DONE, closed_episode_wait_us=minutes(14),
            ack='terminal')),
        ('done without a closed wait', kupfer(
            state=DashboardRowState.DONE, ack='terminal')),
        ('legacy', legacy_row()),
        ('lobby', orbit_50()),
        ('review open', kupfer_50(review_open=True)),
        ('review not installed', kupfer(review_available=False)),
        ('conflicts', kupfer_50(conflicts=(
            conflict_of_kupfer(), external_conflict('Echo')))),
        ('external only', neon(conflicts=(external_conflict(),))),
        ('no orga, no game', kupfer(orga_names=(), game=None)),
        ('open sides', kupfer(contestant_names=())),
        ('ready unknown', neon(readiness_available=False)),
        ('new episode', kupfer(
            has_prior_episode=True, ack='below',
            status_note=DashboardStatusNote(code=STATUS_NOTE_CORRECTED_REOPENED),
            tier=GREEN, total_active_wait_us=minutes(3),
            alert_interval_us=minutes(3))),
    ]  # fmt: skip


EVERY_STATE = _every_state_rows()


@pytest.mark.parametrize('surface', ['admin', 'site'])
@pytest.mark.parametrize(
    'row', [r for _, r in EVERY_STATE], ids=[n for n, _ in EVERY_STATE]
)
def test_dashboard_rows_render_under_strict_undefined_for_every_row_state(
    env, row, surface
):
    html = render(env, context_of([row], surface=surface))

    (li,) = rows_of(html)
    article = li.find('article')
    heading = li.find('h3')
    assert article.attrs['aria-labelledby'] == heading.attrs['id']
    assert li.attrs['id'].startswith('lt-row-')
    assert heading.find('a') is not None


def test_rows_render_with_a_refusal_for_a_row_that_is_not_listed(env):
    row = timed(kupfer, YELLOW, 20, 20, ack='offered')
    context = context_of([row])
    action = _action('ack', None, 'stale', draft_target=False, draft='x')

    with_action = render(env, context, action)
    without = render(env, context, None)

    assert with_action == without


def test_the_row_macros_import_without_a_context(env):
    template = env.from_string(
        "{% from 'common/lan_tournament/_dashboard_rows.html' import "
        'render_rows, render_row %}'
        '{{ render_rows(dashboard, "count-heading") }}'
        '{{ render_row(dashboard.rows[0], dashboard.labels) }}'
    )
    context = context_of([timed(kupfer, GREEN, 5, 5)])

    html = template.render(dashboard=context)

    assert len(rows_of(html)) == 2
    assert 'aria-labelledby="count-heading"' in html


def test_an_empty_page_renders_no_list(env):
    assert render(env, context_of([])).strip() == ''


def test_rows_render_in_the_default_language_too(env):
    for frame in EXPECTED:
        assert _render_frame(env, frame)


# ---------------------------------------------- distinct, truthful labels


def test_complete_partial_paused_ffa_and_no_history_rows_have_distinct_truthful_labels(
    env, german
):
    complete = facts(rows_of(render(env, context_of([
        kupfer_50(review_open=True)])))[0])  # fmt: skip
    partial = facts(rows_of(render(env, context_of([partial_row()])))[0])
    paused = facts(rows_of(render(env, context_of([paused_row()])))[0])
    ffa = facts(rows_of(render(env, context_of([orbit_50()])))[0])
    legacy = facts(rows_of(render(env, context_of([legacy_row()])))[0])

    cells = [r['tier'][:2] for r in (complete, partial, paused, ffa, legacy)]
    assert len({tuple(c) for c in cells}) == 5
    assert cells == [
        ['▲', 'Verzögerung prüfen'],
        ['◌', 'Unvollständig'],
        ['❚❚', 'Pausiert'],
        ['○', 'Unter Warnschwelle'],
        ['?', 'Zeit unbekannt'],
    ]

    # Paused: frozen values, no tier colour, no check, the pin stays.
    assert paused['tier'] == [
        '❚❚',
        'Pausiert',
        '5 min',
        'Alarmintervall, eingefroren',
    ]
    assert paused['tier_class'] == ['t-paused']
    assert paused['times'][0] == [
        'Aktive Wartezeit gesamt',
        '20 min, eingefroren',
    ]
    assert paused['reason'] == 'Pausiert – Prüfen nicht möglich.'
    assert paused['opener'] is None and paused['form'] is False
    assert paused['pin'] == 'Pin entfernen'

    # Partial: no opponent, no readiness line, no invented time.
    assert partial['title'] == 'Nachtbus gegen Gegner noch offen'
    assert partial['ready'] is None
    assert partial['times'][0] == [
        'Aktive Wartezeit gesamt',
        'Noch nicht fällig',
    ]
    assert partial['times'][1] == ['Besetzt seit', 'Noch nicht besetzt']

    # A lobby has no A/B readiness: unavailable, never "nobody ready".
    assert ffa['ready'] == ['Bereitschaft', 'Nicht verfügbar (Lobby-Format)']
    assert ffa['lobby'] == ['Tess', 'Echo', 'Lio', 'Vale']

    # No history: every unknown fact says so, none is a time or a zero.
    (legacy_li,) = rows_of(render(env, context_of([legacy_row()])))
    unknown_values = [
        dd
        for dd in legacy_li.find_all('dd')
        if dd.text == 'Historischer Zeitpunkt unbekannt'
    ]
    assert len(unknown_values) == 2
    assert all(dd.find('span', 'ltd-na') for dd in unknown_values)
    unknown = 'Historischer Zeitpunkt unbekannt'
    assert legacy['ready'] == ['Bereitschaft', 'Nicht verfügbar']
    assert legacy['times'][0] == ['Aktive Wartezeit gesamt', 'Nicht verfügbar']
    assert legacy['times'][1] == ['Besetzt seit', unknown]
    assert legacy['times'][2] == ['Letzte Match-Änderung', unknown]
    assert legacy['reason'] == 'Ohne Zeitbasis kein Prüfen.'
    assert complete['ready'] != ffa['ready'] != legacy['ready']  # fmt: skip

    # The five times keep their own labels.
    labels = [label for label, _ in complete['times']]
    assert labels == [
        'Aktive Wartezeit gesamt',
        'Besetzt seit',
        'Fällig seit (diese Episode)',
        'Letzte Match-Änderung',
        'Erstellt',
    ]


def test_the_tier_cell_and_the_ready_line_are_separate_and_never_colour_only(
    env, german
):
    for _, row in EVERY_STATE:
        (li,) = rows_of(render(env, context_of([row])))

        tier = li.find('div', 'ltd-tier')
        icon = tier.find('span', 'ltd-ti')
        assert icon.attrs.get('aria-hidden') == 'true' and icon.text
        assert tier.find('span', 'ltd-tn').text

        ready = li.find('p', 'ltd-ready')
        if ready is None:
            continue
        # Readiness never wears a tier class, and the tier cell never
        # speaks of readiness.
        assert not [
            c
            for node in [ready, *ready.walk()]
            for c in node.classes
            if re.fullmatch(r't-[a-z]+', c)
        ]
        assert 'bereit' not in ' '.join(tier.strings).lower()
        summary = ready.find('b', 'ltd-rs')
        for side in ready.find_all('span', 'ltd-rd'):
            mark = side.find('i')
            assert mark.attrs.get('aria-hidden') == 'true'
            assert mark.text in ('✓', '○') and len(side.strings) == 2
        if summary is not None:
            level = int(re.search(r'rs-(\d)', summary.attrs['class']).group(1))
            ticks = [s.find('i').text for s in ready.find_all('span', 'ltd-rd')]
            assert ticks.count('✓') == level


def test_every_signal_carries_a_text_and_an_icon_unless_it_is_unavailable(
    env, german
):
    seen = set()
    for _, row in EVERY_STATE:
        (li,) = rows_of(render(env, context_of([row])))
        for signal in li.find_all('span', 'ltd-sig'):
            kind = signal.attrs['class'].split()[-1]
            seen.add(kind)
            icon = signal.find('i')
            if kind == 's-na':
                assert icon is None
            else:
                assert icon is not None and icon.attrs['aria-hidden'] == 'true'
                assert icon.text
            assert signal.strings[-1]

    assert seen == {'s-pin', 's-conf', 's-rev', 's-na', 's-ep'}


# ------------------------------------------------------- refusals (no-JS)


def _action(
    kind,
    match_id,
    error,
    *,
    draft_target,
    draft=None,
    message='Der Stand hat sich geändert.',
    detail=None,
    field_error=None,
) -> dict[str, Any]:
    """The refusal of a native POST, as the routes hand it to the page."""
    return {
        'kind': kind,
        'match_id': match_id,
        'error': error,
        'message': message,
        'detail': detail,
        'draft': draft,
        'draft_target': draft_target,
        'field_error': field_error,
    }


def _reopen(context, index, draft, error_text=None) -> None:
    """Reopen the form of a row with its draft, as the routes do."""
    form = context['rows'][index]['ack']['form']
    form.update(
        {
            'open': True,
            'draft': draft,
            'counter_text': format_comment_counter(len(draft)),
            'is_over': len(draft) > 500,
            'error_text': error_text,
        }
    )


def test_a_native_refusal_reopens_only_the_form_of_the_row_it_names(env):
    first = timed(kupfer, YELLOW, 20, 20, ack='offered')
    second = timed(neon, YELLOW, 25, 25, ack='offered')
    context = context_of([first, second])
    target = context['rows'][0]['match_id']
    _reopen(context, 0, 'Mein Entwurf <b>bleibt</b>')
    action = _action(
        'ack',
        target,
        'stale',
        draft_target=True,
        draft='Mein Entwurf <b>bleibt</b>',
        message='Der Stand hat sich geändert.',
        detail='Mara hat diese Verzögerung bereits festgehalten.',
    )

    rows = rows_of(render(env, context, action))

    named, other = rows
    form = named.find('form', 'ltd-form')
    assert 'data-open' in form.attrs
    assert form.find('textarea').raw_text == 'Mein Entwurf <b>bleibt</b>'
    alert = form.find('div', 'ltd-fmsg')
    assert alert.attrs['role'] == 'alert'
    assert alert.find('b').text == 'Der Stand hat sich geändert.'
    assert (
        alert.find('p').text
        == 'Mara hat diese Verzögerung bereits festgehalten.'
    )
    assert (
        named.find('button', **{'data-ack-open': True}).attrs['aria-expanded']
        == 'true'
    )

    other_form = other.find('form', 'ltd-form')
    assert 'data-open' not in other_form.attrs
    assert other_form.find('div', 'ltd-fmsg') is None
    assert other_form.find('textarea').raw_text == ''
    assert (
        other.find('button', **{'data-ack-open': True}).attrs['aria-expanded']
        == 'false'
    )
    assert other.find('p', 'ltd-err') is None


def test_a_field_error_shows_at_the_comment_and_not_in_the_form_alert(
    env, german
):
    row = timed(kupfer, YELLOW, 20, 20, ack='offered')
    context = context_of([row])
    draft = 'ä' * 501
    text = (
        'Kommentar ist zu lang: 501 von höchstens 500 Zeichen.'
        ' Nichts wurde gespeichert.'
    )
    _reopen(context, 0, draft, error_text=text)
    action = _action(
        'ack',
        context['rows'][0]['match_id'],
        'invalid',
        draft_target=True,
        draft=draft,
        message=text,
        field_error={'field': 'comment', 'text': text},
    )

    (li,) = rows_of(render(env, context, action))

    form = li.find('form', 'ltd-form')
    area = form.find('textarea')
    error = form.find('p', 'ltd-err')
    assert area.attrs['aria-invalid'] == 'true'
    assert error.text == text
    assert error.attrs['id'] in area.attrs['aria-describedby'].split()
    assert form.find('div', 'ltd-fmsg') is None
    assert area.raw_text == draft
    counter = form.find('p', 'ltd-cnt')
    assert counter.attrs['class'].split() == ['ltd-cnt', 'is-over']
    assert counter.text == '501 / 500 Zeichen · nur Text'


def test_a_form_error_of_another_field_shows_in_the_form_alert(env):
    row = timed(kupfer, YELLOW, 20, 20, ack='offered')
    context = context_of([row])
    _reopen(context, 0, 'x', error_text=None)
    action = _action(
        'ack',
        context['rows'][0]['match_id'],
        'invalid',
        draft_target=True,
        draft='x',
        message='Ungültiger Wert.',
        field_error={'field': 'revision', 'text': 'Ungültiger Wert.'},
    )

    (li,) = rows_of(render(env, context, action))

    alert = li.find('form', 'ltd-form').find('div', 'ltd-fmsg')
    assert alert.find('b').text == 'Ungültiger Wert.'
    assert alert.find('p') is None
    assert li.find('textarea').attrs.get('aria-invalid') is None


def test_a_pin_refusal_shows_next_to_the_pin_of_the_row_it_names(env):
    first = timed(kupfer, YELLOW, 20, 20, ack='offered')
    second = neon()
    context = context_of([first, second])
    action = _action(
        'pin',
        context['rows'][1]['match_id'],
        'stale',
        draft_target=False,
        message='Der Stand hat sich geändert. Bitte aktualisieren.',
    )

    unnamed, named = rows_of(render(env, context, action))

    alert = named.find('div', 'ltd-act').find('p', 'ltd-err')
    assert alert.attrs['role'] == 'alert'
    assert alert.text == 'Der Stand hat sich geändert. Bitte aktualisieren.'
    assert unnamed.find('p', 'ltd-err') is None
    assert named.find('form', 'ltd-form').find('div', 'ltd-fmsg') is None


def test_a_pin_refusal_is_never_shown_on_a_row_without_a_pin_control(env):
    done = kupfer(state=DashboardRowState.DONE, ack='terminal')
    context = context_of([done])
    action = _action(
        'pin',
        context['rows'][0]['match_id'],
        'refused',
        draft_target=False,
        message='Abgeschlossen.',
    )

    (li,) = rows_of(render(env, context, action))

    assert li.find('p', 'ltd-err') is None
    assert li.find('button', **{'data-pin': True}) is None


# ---------------------------------------------------------------- forms

FORM_NAMES = {
    'ack': ['csrf_token', 'episode', 'revision', 'return'],
    'pin': ['csrf_token', 'revision', 'return', 'pinned'],
}


@pytest.mark.parametrize(
    ('surface', 'ack_path', 'pin_path'),
    [
        (
            'admin',
            '/lan-tournaments/for_party/{party}/dashboard/matches/{id}/ack',
            '/lan-tournaments/for_party/{party}/dashboard/matches/{id}/pin',
        ),
        (
            'site',
            '/lan-tournaments/orga-dashboard/matches/{id}/ack',
            '/lan-tournaments/orga-dashboard/matches/{id}/pin',
        ),
    ],
)
def test_forms_carry_csrf_episode_and_revision_but_no_client_actor(
    env, surface, ack_path, pin_path
):
    row = kupfer_50(ack_revision=7, pin_revision=3)
    context = context_of([row], surface=surface, token='tok-123')
    match_id = context['rows'][0]['match_id']

    (li,) = rows_of(render(env, context))

    ack = li.find('form', 'ltd-form')
    pin = li.find('form', 'ltd-pin')
    for form, kind, path in [(ack, 'ack', ack_path), (pin, 'pin', pin_path)]:
        assert form.attrs['method'] == 'post'
        assert form.attrs['action'] == path.format(party=PARTY, id=match_id)
        hidden = {
            node.attrs['name']: node.attrs['value']
            for node in form.find_all('input')
            if node.attrs['type'] == 'hidden'
        }
        assert sorted(hidden) == sorted(FORM_NAMES[kind])
        assert hidden['csrf_token'] == 'tok-123'
        assert hidden['return'] == context['return_value']
        # Nothing the client could use to name an actor or a scope.
        inputs = [n.attrs['name'] for n in form.find_all('input')]
        assert not [
            n
            for n in inputs
            if re.search('user|actor|party|tournament|match', n)
        ]

    names = {n.attrs['name'] for n in ack.find_all('textarea')}
    assert names == {'comment'}
    hidden_ack = {
        n.attrs['name']: n.attrs['value'] for n in ack.find_all('input')
    }
    assert hidden_ack['revision'] == '7'
    assert hidden_ack['episode'] == str(row.episode_id)
    hidden_pin = {
        n.attrs['name']: n.attrs['value'] for n in pin.find_all('input')
    }
    assert hidden_pin['revision'] == '3'
    assert hidden_pin['pinned'] == 'false'  # the row is pinned: next is unpin


def test_the_pin_toggles_towards_the_other_state(env):
    pinned = kupfer(ack='below')
    free = kupfer(ack='below', pinned_at=None, pinned_by_name=None)
    context = context_of([pinned, free])

    first, second = rows_of(render(env, context))

    def pin_value(li):
        form = li.find('form', 'ltd-pin')
        return {
            n.attrs['name']: n.attrs['value'] for n in form.find_all('input')
        }['pinned']

    assert pin_value(first) == 'false'
    assert pin_value(second) == 'true'


def test_the_ack_form_counts_code_points_and_has_no_maxlength(env, german):
    context = context_of([timed(kupfer, YELLOW, 20, 20, ack='offered')])

    html = render(env, context)

    (li,) = rows_of(html)
    assert 'maxlength' not in html
    form = li.find('form', 'ltd-form')
    area = form.find('textarea')
    assert area.attrs['rows'] == '3'
    counter = form.find('p', 'ltd-cnt')
    assert counter.text == '0 / 500 Zeichen · nur Text'
    assert counter.attrs['class'].split() == ['ltd-cnt']
    help_text = form.find('p', 'ltd-help')
    assert area.attrs['aria-describedby'].split() == [
        help_text.attrs['id'],
        counter.attrs['id'],
    ]
    assert form.find('label').attrs['for'] == area.attrs['id']
    assert form.find('label').text == 'Kommentar (optional)'
    assert form.find('h4').attrs['id'] == form.attrs['aria-labelledby']
    assert form.find('h4').text == 'Verzögerung geprüft – festhalten'
    assert help_text.text == (
        'Hält fest, dass du die Verzögerung geprüft hast. Setzt nur das'
        ' Alarmintervall zurück, nicht die gesamte Wartezeit. Sichtbar für'
        ' alle Orgas dieses Turniers, nicht für Spielende.'
    )
    buttons = form.find('div', 'ltd-frow').find_all('button')
    assert [b.text for b in buttons] == ['Prüfung festhalten', 'Abbrechen']
    assert [b.attrs['type'] for b in buttons] == ['submit', 'button']

    # An astral character is one character: the server counts code points.
    _reopen(context, 0, '😀' * 3)
    (reopened,) = rows_of(render(env, context))
    assert reopened.find('p', 'ltd-cnt').text == '3 / 500 Zeichen · nur Text'


def test_without_a_script_the_form_is_open_and_the_script_controls_are_hidden(
    env,
):
    context = context_of([timed(kupfer, YELLOW, 20, 20, ack='offered')])

    (li,) = rows_of(render(env, context))

    form = li.find('form', 'ltd-form')
    opener = li.find('button', **{'data-ack-open': True})
    cancel = li.find('button', **{'data-ack-cancel': True})
    assert 'hidden' not in form.attrs and 'data-open' not in form.attrs
    assert 'hidden' in opener.attrs and 'hidden' in cancel.attrs
    assert opener.attrs['aria-controls'] == form.attrs['id']
    assert opener.attrs['aria-expanded'] == 'false'
    assert opener.attrs['type'] == 'button'
    # The one native way to send it stays visible.
    submit = form.find('button', type='submit')
    assert 'hidden' not in submit.attrs


# ----------------------------------------------------------------- links


@pytest.mark.parametrize('surface', ['admin', 'site'])
def test_match_and_tournament_links_carry_the_validated_return(
    env, german, surface
):
    query = _query(view='all', sort='wait', page=2)
    counterpart = replace(conflict_of_kupfer().visible_refs[0], list_page=3)
    conflict = replace(conflict_of_kupfer(), visible_refs=(counterpart,))
    context = context_of(
        [kupfer_50(conflicts=(conflict,)), neon()],
        surface=surface,
        query=query,
    )
    expected = context['return_value']
    assert 'view=all' in expected and 'sort=wait' in expected

    html = render(env, context)

    anchors = parse(html).find_all('a')
    page_links = [a for a in anchors if a.text == 'In dieser Liste: Seite 3']
    others = [a for a in anchors if a not in page_links]
    # Per row: tournament and match link in the identity, both again in the
    # actions; and the counterpart's match link of the conflict.
    assert len(page_links) == 1
    assert len(others) == 2 * 4 + 1
    for anchor in others:
        parts = urlsplit(anchor.attrs['href'])
        assert parse_qs(parts.query)['return'] == [expected], anchor.attrs
    # The page link of the counterpart is the list URL, not a match URL.
    parts = urlsplit(page_links[0].attrs['href'])
    assert parse_qs(parts.query)['page'] == ['3']
    assert parts.fragment == f'lt-row-{counterpart.match_id}'
    for node in parse(html).find_all('input', name='return'):
        assert node.attrs['value'] == expected


# --------------------------------------------------------- hidden state

HOSTILE = '<script>alert(1)</script>"\'&</textarea><img src=x onerror=alert(1)>'
UUID = re.compile(
    r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', re.I
)
ALLOWED_TAGS = {
    'li', 'article', 'div', 'span', 'p', 'a', 'b', 'i', 'q', 'h3', 'h4',
    'ul', 'ol', 'dl', 'dt', 'dd', 'form', 'input', 'button', 'label',
    'textarea',
}  # fmt: skip
ALLOWED_ATTRIBUTES = {
    'class', 'id', 'href', 'role', 'type', 'name', 'value', 'method',
    'action', 'for', 'rows', 'hidden', 'tabindex', 'aria-labelledby',
    'aria-hidden', 'aria-expanded', 'aria-controls', 'aria-describedby',
    'aria-invalid', 'aria-label', 'data-ack', 'data-ack-open',
    'data-ack-cancel', 'data-hist', 'data-pin', 'data-open',
}  # fmt: skip
MODIFIER = re.compile(r'(t|s|rs|is)-[a-z0-9]+|pri')


def _all_markup(env) -> list[tuple[str, str, Node]]:
    """Every row of the frames and of the states, with its own HTML."""
    out = []
    for frame in EXPECTED:
        for clock, panel_rows in frame_panels()[frame]:
            for row in panel_rows:
                html = render(env, context_of([row], clock=clock))
                out.append((frame, html, parse(html)))
    for name, row in EVERY_STATE:
        html = render(env, context_of([row]))
        out.append((name, html, parse(html)))
    return out


def test_the_markup_has_only_ltd_classes_and_no_hidden_state_carriers(env):
    for name, _, dom in _all_markup(env):
        for node in dom.walk():
            assert node.tag in ALLOWED_TAGS, (name, node.tag)
            for attribute, value in node.attrs.items():
                assert attribute in ALLOWED_ATTRIBUTES, (name, attribute)
                if attribute.startswith('data-'):
                    assert value is None, (name, attribute)
            for cls in node.classes:
                assert cls.startswith('ltd-') or MODIFIER.fullmatch(cls), (
                    name,
                    cls,
                )
            if 'hidden' in node.attrs and node.tag != 'input':
                assert node.tag in ('button', 'ol'), (name, node.tag)
                assert (
                    'data-ack-open' in node.attrs
                    or 'data-ack-cancel' in node.attrs
                    or 'ltd-hist' in node.classes
                ), (name, node.tag)
            if node.tag == 'input':
                assert node.attrs['type'] == 'hidden'
                assert node.parent.tag == 'form'


def test_every_id_in_the_markup_belongs_to_the_row_or_an_authorized_counterpart(
    env,
):
    hidden_user = UserID(_id())
    external = replace(external_conflict('Echo'), user_id=hidden_user)
    authorized = conflict_of_kupfer()
    row = kupfer_50(conflicts=(authorized, external))
    context = context_of([row, orbit_50()])

    html = render(env, context)

    found = {m.lower() for m in UUID.findall(html)}
    allowed = {
        str(row.match_id),
        str(row.tournament_id),
        str(row.episode_id),
        str(authorized.visible_refs[0].match_id),
    }
    lobby = context['rows'][1]
    allowed |= {
        lobby['match_id'],
        lobby['tournament']['id'],
        lobby['ack']['form']['episode'] if lobby['ack']['form'] else '',
    }
    allowed.discard('')
    assert found <= allowed, found - allowed
    # The counterpart the viewer may see is linked; nobody else is named.
    assert str(authorized.visible_refs[0].match_id) in found
    assert str(hidden_user) not in html
    assert str(authorized.visible_refs[0].tournament_id) not in html
    assert str(authorized.user_id) not in html


def test_the_external_conflict_block_is_the_same_whatever_hides_behind_it(env):
    one = neon(conflicts=(external_conflict('Nori'),))
    many = kupfer(
        conflicts=(external_conflict('Nori'),),
        tournament_name='Ein ganz anderes Turnier',
        contestant_names=('A', 'B'),
    )
    both = kupfer_50(
        conflicts=(conflict_of_kupfer(), external_conflict('Nori'))
    )

    block = re.compile(r'<div class="ltd-conf is-ext">.*?</div>', re.S)
    found = [
        block.findall(render(env, context_of([row])))
        for row in (one, many, both)
    ]

    assert [len(f) for f in found] == [1, 1, 1]
    assert found[0] == found[1] == found[2]
    assert found[0][0] == (
        '<div class="ltd-conf is-ext"><p><i aria-hidden="true">⇄</i>'
        '<b>Nori</b> is needed in a match outside your tournaments at the'
        ' same time.</p></div>'
    )


def test_the_ack_comment_is_escaped_plain_text(env):
    comment = f'{HOSTILE} zweite Zeile\n<b>fett</b>'
    ack = ack_summary(HOSTILE, '14:15', comment)
    older = ack_summary(f'{HOSTILE}2', '14:10', f'<i>{HOSTILE}</i>')
    row = kupfer_50(
        tournament_name=HOSTILE,
        game=HOSTILE,
        contestant_names=(HOSTILE, f'{HOSTILE}b'),
        orga_names=(HOSTILE,),
        pinned_by_name=HOSTILE,
        latest_acknowledgement=ack,
        recent_acknowledgements=(ack, older),
        acknowledgement_count=2,
        conflicts=(
            replace(
                conflict_of_kupfer(),
                user_display_name=HOSTILE,
                via_team_name=HOSTILE,
            ),
        ),
    )
    context = context_of([row, orbit(contestant_names=(HOSTILE, 'x'))])

    html = render(env, context)

    assert '<script' not in html and '<img' not in html
    assert '<b>fett</b>' not in html and '<i>&lt;script' not in html
    assert 'onerror=alert(1)>' not in html
    dom = parse(html)
    assert not dom.find_all('script') and not dom.find_all('img')
    (first, second) = dom.find_all('li', 'ltd-row')
    assert first.find('p', 'ltd-recc').text == f'„{comment}“'.replace('\n', ' ')
    assert first.find('p', 'ltd-ctx').strings == [HOSTILE, HOSTILE, 'Knockout']
    assert HOSTILE in first.find('b').text or HOSTILE in ' '.join(first.strings)
    assert first.find('ol', 'ltd-hist').find_all('li')[1].strings[-1] == (
        f'„<i>{HOSTILE}</i>“'
    )
    assert second.find('ul', 'ltd-lobby').find_all('li')[0].text == HOSTILE
    # The same text, read back from a form field, is exactly the draft.
    _reopen(context, 0, HOSTILE)
    again = rows_of(render(env, context))[0]
    assert again.find('textarea').raw_text == HOSTILE


def test_a_comment_keeps_its_line_breaks_for_the_stylesheet(env):
    ack = ack_summary('Mara', '14:15', 'eins\nzwei')
    row = kupfer_50(
        latest_acknowledgement=ack,
        recent_acknowledgements=(ack,),
        conflicts=(),
    )

    html = render(env, context_of([row]))

    (li,) = rows_of(html)
    assert li.find('p', 'ltd-recc').find('q').raw_text == 'eins\nzwei'


# --------------------------------------------------------------- history


def test_the_history_disclosure_is_collapsed_and_wired_to_its_list(env, german):
    acks = tuple(
        ack_summary(actor, clock, comment, revision=index)
        for index, (actor, clock, comment) in enumerate(
            [
                ('Jonas', '14:50', 'drei'),
                ('Mara', '14:30', None),
                ('Alex', '14:15', 'eins'),
            ]
        )
    )
    row = timed(
        kupfer,
        YELLOW,
        70,
        20,
        latest_acknowledgement=acks[0],
        recent_acknowledgements=acks,
        acknowledgement_count=5,
        ack='offered',
    )

    (li,) = rows_of(render(env, context_of([row])))

    button = li.find('button', 'ltd-disc')
    history = li.find('ol', 'ltd-hist')
    assert button.attrs['type'] == 'button'
    assert button.attrs['aria-expanded'] == 'false'
    assert button.attrs['aria-controls'] == history.attrs['id']
    assert 'hidden' in history.attrs
    assert button.text == 'Letzte Prüfungen anzeigen (5 in dieser Episode)'
    assert [n.strings for n in history.find_all('li')] == [
        ['Jonas', '· 14:50 · aktuell', '„drei“'],
        ['Mara', '· 14:30', 'ohne Kommentar'],
        ['Alex', '· 14:15', '„eins“'],
    ]
    # The latest check is outside the disclosure and always visible.
    record = li.find('div', 'ltd-rec')
    assert 'hidden' not in record.attrs
    assert record.find('p', 'ltd-rech').strings == [
        'Zuletzt geprüft von Jonas um 14:50',
        '▲',
        'Erneute Verzögerung – nochmals prüfen',
    ]
    assert history.parent is record
    assert button.parent is record


def test_a_single_check_has_no_disclosure_and_a_green_row_has_no_follow_badge(
    env,
):
    mara = ack_summary('Mara', '14:15', None)
    green = timed(
        kupfer,
        GREEN,
        15,
        0,
        latest_acknowledgement=mara,
        recent_acknowledgements=(mara,),
        acknowledgement_count=1,
        ack='recent',
    )

    (li,) = rows_of(render(env, context_of([green])))

    assert li.find('button', 'ltd-disc') is None
    assert li.find('ol', 'ltd-hist') is None
    assert li.find('span', 'ltd-follow') is None
    assert li.find('p', 'ltd-recc') is None
    assert li.find('div', 'ltd-rec').attrs['tabindex'] == '-1'


@pytest.mark.parametrize(
    ('tier', 'cls'), [(YELLOW, 'is-yellow'), (RED, 'is-red')]
)
def test_the_follow_badge_names_the_tier_it_follows(env, tier, cls):
    row = replace(
        kupfer_50(),
        tier=tier,
        alert_interval_us=minutes(50 if tier is RED else 20),
    )

    (li,) = rows_of(render(env, context_of([row])))

    badge = li.find('span', 'ltd-follow')
    assert badge.attrs['class'].split() == ['ltd-follow', cls]
    assert badge.find('i').text == '▲'


def test_the_second_orga_sees_the_first_actor_next_to_the_renewed_alert(
    env, german
):
    (li,) = rows_of(render(env, context_of([kupfer_50()])))

    record = li.find('div', 'ltd-rec')
    tier = li.find('div', 'ltd-tier')
    assert record.find('p', 'ltd-rech').strings[0] == (
        'Zuletzt geprüft von Mara um 14:15'
    )
    assert tier.strings[1] == 'Verzögerung prüfen'
    assert li.find('dl', 'ltd-times').find('dd').text == '30 min'
    assert tier.strings[2:] == ['15 min', 'Alarmintervall']


# ------------------------------------------------------------- the source


def _skeleton(html: str) -> str:
    """Blank what only a surface decides: its URLs and the return value."""
    html = re.sub(r'(href|action)="[^"]*"', r'\1="*"', html)
    return re.sub(r'name="return" value="[^"]*"', 'name="return"', html)


def test_the_two_surfaces_render_one_operational_vocabulary(env):
    rows = [kupfer_50(), neon_50(), orbit_50(), paused_row(), legacy_row()]

    admin = render(env, context_of(rows, surface='admin'))
    site = render(env, context_of(rows, surface='site'))

    assert admin != site
    assert _skeleton(admin) == _skeleton(site)


def test_the_template_source_has_no_translation_call_and_no_unsafe_output():
    source = (TEMPLATES / ROWS_TEMPLATE).read_text(encoding='utf-8')

    assert not re.search(r'\b_\(|gettext', source)
    assert '|safe' not in source and 'Markup' not in source
    assert 'autoescape' not in source
    assert '<script' not in source and '<style' not in source
    assert 'javascript:' not in source
    assert not re.search(r'\bon[a-z]+=', source)
    for value in re.findall(r'class="([^"{]*)"', source):
        for cls in value.split():
            assert cls.startswith('ltd-') or MODIFIER.fullmatch(cls), cls
