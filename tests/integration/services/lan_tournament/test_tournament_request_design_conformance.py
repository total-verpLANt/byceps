"""Compare rendered request pages with the drafts' computed styles."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, UTC
import json
import mimetypes
import os
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlsplit

from babel.messages.mofile import write_mo
from babel.messages.pofile import read_po
from flask_babel import get_babel
import pytest

from byceps.byceps_app import BycepsApp
from byceps.services.lan_tournament import (
    tournament_request_service,
    tournament_service,
)
from byceps.services.lan_tournament.models.contestant_type import (
    ContestantType,
)
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.party.models import PartyID
from byceps.services.site.models import Site, SiteID
from byceps.services.ticketing import ticket_creation_service
from byceps.services.user.models import User

from tests.helpers import create_site, http_client, log_in_user


HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[3]
DESIGN_DIR = HERE / 'f17_design'
PAIRS_PATH = DESIGN_DIR / 'pairs.json'
GOLDEN_PATH = DESIGN_DIR / 'golden.json'
TRANSLATIONS_DIR = REPO_ROOT / 'byceps' / 'translations'

CHROMIUM_PATH = '/usr/bin/chromium'


def _playwright_importable() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return False
    return True


_ENV_OK = os.environ.get('LT_DESIGN_CONFORMANCE') == '1'
_PLAYWRIGHT_OK = _playwright_importable()
_CHROMIUM_OK = Path(CHROMIUM_PATH).exists()
_GATE_OPEN = _ENV_OK and _PLAYWRIGHT_OK and _CHROMIUM_OK

_SKIP_REASON = (
    'design-conformance harness needs LT_DESIGN_CONFORMANCE=1, an '
    'importable playwright, and /usr/bin/chromium; set all three to '
    'run it'
)

_PAIRS_DATA: dict[str, Any] = json.loads(PAIRS_PATH.read_text(encoding='utf-8'))
FRAMES: dict[str, Any] = _PAIRS_DATA['frames']
PAIRS: dict[str, Any] = _PAIRS_DATA['pairs']
FRAME_KEYS: list[str] = sorted(FRAMES)

_GOLDEN: dict[str, Any] = (
    json.loads(GOLDEN_PATH.read_text(encoding='utf-8'))
    if GOLDEN_PATH.exists()
    else {}
)

PARTY_ID = PartyID('lt-design-conformance-party')
SITE_ID = SiteID('totalverplant-36')
ADMIN_SERVER_NAME = 'admin.acmecon.test'

_R4_REJECT_REASON = (
    'Samstagnachmittag ist die Bühne mit dem Hauptturnier belegt. '
    'Reich es gern für Sonntagvormittag neu ein.'
)

# Browser-side measurement; mirrors `generate_golden.EXTRACT_JS`.
_OURS_EXTRACT_JS = r"""
([scope, sel, propSpecs]) => {
  const root = document.querySelector(scope);
  if (!root) return null;
  const m = sel.match(/^(.*)::(before|after|placeholder)$/);
  let base = sel.trim();
  let pseudo = null;
  if (m) {
    base = m[1].trim();
    pseudo = '::' + m[2];
  }
  let el;
  if (base === '') {
    el = root;
  } else if (root.matches && (() => {
    try {
      return root.matches(base);
    } catch (e) {
      return false;
    }
  })()) {
    el = root;
  } else {
    el = root.querySelector(base);
  }
  if (!el) return null;

  function contentBoxRatio(prop) {
    const parent = el.parentElement;
    if (!parent) return null;
    const pcs = getComputedStyle(parent);
    const padLeft = parseFloat(pcs.paddingLeft) || 0;
    const padRight = parseFloat(pcs.paddingRight) || 0;
    const padTop = parseFloat(pcs.paddingTop) || 0;
    const padBottom = parseFloat(pcs.paddingBottom) || 0;
    const heightish = prop === 'height' || prop === 'min-height' || prop === 'max-height';
    const base_ = heightish
      ? parent.clientHeight - padTop - padBottom
      : parent.clientWidth - padLeft - padRight;
    if (!base_) return null;
    const cs = getComputedStyle(el, pseudo || undefined);
    const value = parseFloat(cs.getPropertyValue(prop));
    if (Number.isNaN(value)) return null;
    return value / base_;
  }

  const cs = getComputedStyle(el, pseudo || undefined);
  const out = {};
  for (const [prop, relative] of propSpecs) {
    if (relative && !pseudo && (prop === 'width' || prop === 'height' || prop === 'min-width')) {
      const ratio = contentBoxRatio(prop);
      out[prop] = ratio === null ? cs.getPropertyValue(prop) : String(ratio);
    } else {
      out[prop] = cs.getPropertyValue(prop);
    }
  }
  return out;
}
"""

_COLOR_FUNC_RE = re.compile(
    r'^color\(srgb\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)(?:\s*/\s*([\d.]+))?\s*\)$'
)
_PX_RE = re.compile(r'^-?\d+(\.\d+)?px$')


def _normalize_color(value: str) -> str:
    match = _COLOR_FUNC_RE.match(value.strip())
    if not match:
        return value.strip()
    r, g, b, a = match.groups()
    r_i, g_i, b_i = (round(float(x) * 255) for x in (r, g, b))
    if a is not None and abs(float(a) - 1) > 1e-6:
        return f'rgba({r_i}, {g_i}, {b_i}, {round(float(a), 3)})'
    return f'rgb({r_i}, {g_i}, {b_i})'


def _normalize_font_family(value: str) -> str:
    """Reduce a `font-family` value to its primary family."""
    primary = value.split(',')[0].strip()
    return primary.strip('"\'').strip().lower()


def _values_match(
    golden_computed: str, ours_computed: str, relative: bool, prop: str = ''
) -> bool:
    if relative:
        try:
            return abs(float(golden_computed) - float(ours_computed)) <= 0.01
        except ValueError:
            return golden_computed == ours_computed

    if prop == 'font-family':
        return _normalize_font_family(
            golden_computed
        ) == _normalize_font_family(ours_computed)

    golden_norm = _normalize_color(golden_computed)
    ours_norm = _normalize_color(ours_computed)
    if golden_norm == ours_norm:
        return True
    if _PX_RE.match(golden_norm) and _PX_RE.match(ours_norm):
        return abs(float(golden_norm[:-2]) - float(ours_norm[:-2])) <= 0.6
    return False


def test_normalize_font_family_ignores_fallback_chain_difference():
    assert _values_match(
        '"Playfair Display", "Old English Text MT", "Times New Roman", serif',
        '"Playfair Display", serif',
        False,
        'font-family',
    )
    assert _values_match(
        '"Cormorant Garamond", "Times New Roman", Georgia, serif',
        '"Cormorant Garamond",Georgia,serif',
        False,
        'font-family',
    )


def test_normalize_font_family_still_reports_primary_family_difference():
    assert not _values_match(
        '"Playfair Display", serif',
        '"Cormorant Garamond", Georgia, serif',
        False,
        'font-family',
    )


@pytest.fixture(scope='module')
def party(make_party, brand):
    # The drafts assume 240 seats; without them the limit caption is empty.
    return make_party(
        brand,
        PARTY_ID,
        'Design Conformance Party',
        max_ticket_quantity=240,
    )


@pytest.fixture(scope='module')
def site(party) -> Site:
    """`totalverplant-36`: the real on-disk `template_overrides` id."""
    return create_site(
        SITE_ID,
        party.brand_id,
        server_name='lt-design-conformance.test',
        party_id=party.id,
    )


@pytest.fixture(scope='session')
def compiled_translations_dir(tmp_path_factory) -> Path:
    """Compile every `messages.po` into a temporary catalog directory."""
    target = tmp_path_factory.mktemp('lt_conformance_translations')
    for po_path in sorted(TRANSLATIONS_DIR.glob('*/LC_MESSAGES/messages.po')):
        locale = po_path.parent.parent.name
        with po_path.open('rb') as f:
            catalog = read_po(f, locale=locale)
        mo_path = target / locale / 'LC_MESSAGES' / 'messages.mo'
        mo_path.parent.mkdir(parents=True)
        with mo_path.open('wb') as f:
            write_mo(f, catalog)
    return target


def _use_translations_from(app: BycepsApp, directory: Path) -> None:
    """Point the app's Flask-Babel at `directory`."""
    babel = get_babel(app)
    babel.default_directories = [str(directory)]
    babel.translation_directories = [str(directory)]
    app.babel_instance.domain_instance.cache.clear()


@pytest.fixture(scope='module')
def conformance_site_app(
    database,
    make_site_app,
    site: Site,
    compiled_translations_dir: Path,
) -> BycepsApp:
    app = make_site_app(site.server_name, site.id)
    _use_translations_from(app, compiled_translations_dir)
    with app.app_context():
        return app


@pytest.fixture(scope='module')
def conformance_admin_app(
    database, make_admin_app, compiled_translations_dir: Path
) -> BycepsApp:
    app = make_admin_app(ADMIN_SERVER_NAME)
    _use_translations_from(app, compiled_translations_dir)
    with app.app_context():
        return app


@pytest.fixture(scope='module')
def admin(make_admin) -> User:
    # `admin.access` is needed for `g.user` to resolve on the admin app.
    return make_admin(
        {
            'admin.access',
            'lan_tournament.view',
            'lan_tournament.request_view',
            'lan_tournament.request_decide',
            'lan_tournament.create',
        },
        screen_name='F17ConformanceAdmin',
    )


@pytest.fixture(scope='module')
def ticket_category(make_ticket_category, party):
    return make_ticket_category(party.id, 'Design Conformance Entry')


@pytest.fixture(scope='module')
def proposer(make_user, ticket_category) -> User:
    """P: holds a valid party ticket."""
    user = make_user(screen_name='F17ConformanceProposer')
    ticket_creation_service.create_ticket(ticket_category, user, user=user)
    return user


@pytest.fixture(scope='module')
def ticketless_user(make_user) -> User:
    """E: no requests, no ticket. Only used for the `empty` dashboard."""
    return make_user(screen_name='F17ConformanceNoRequests')


def _submit(party, proposer_id, **overrides):
    now = datetime.now(UTC)
    kwargs: dict[str, Any] = {
        'party_capacity': None,
        'name': 'Conformance Cup',
        'game': 'Mario Kart 8 Deluxe',
        'game_format': GameFormat.FREE_FOR_ALL,
        'elimination_mode': EliminationMode.SINGLE_ELIMINATION,
        'team_size': 1,
        'participant_limit': 32,
        'preferred_start_time': now + timedelta(days=3),
        'preferred_end_time': now + timedelta(days=3, hours=6),
        'description': (
            'Drei Cups, gemischte Strecken, Items an. Wer zuerst die '
            'Ziellinie sieht, bekommt den Ehrenkeks.'
        ),
        'special_rules': (
            'Blaue Panzer sind verboten. Wer drängelt, fährt die nächste '
            'Runde rückwärts.'
        ),
        'notes': 'Nur die Orga sieht das.',
    }
    kwargs.update(overrides)
    result = tournament_request_service.submit_request(
        party.id, proposer_id, **kwargs
    )
    assert result.is_ok(), result.unwrap_err()
    request, _event = result.unwrap()
    return request


@pytest.fixture(scope='module')
def requests_(party, proposer, admin) -> dict[str, Any]:
    """Submit the five requests the frames render."""
    r1 = _submit(party, proposer.id)
    update_result = tournament_request_service.update_request(
        r1.id,
        proposer.id,
        by='proposer',
        party_capacity=None,
        name=r1.name,
        game=r1.game,
        game_format=r1.game_format,
        elimination_mode=r1.elimination_mode,
        team_size=r1.team_size,
        participant_limit=40,
        preferred_start_time=r1.preferred_start_time,
        preferred_end_time=r1.preferred_end_time,
        description=r1.description,
        special_rules=r1.special_rules,
        notes=r1.notes,
        desired_template=r1.desired_template,
    )
    assert update_result.is_ok(), update_result.unwrap_err()
    r1 = update_result.unwrap()[0]

    r2 = _submit(
        party,
        proposer.id,
        name='Conformance Highscore Cup',
        game_format=GameFormat.HIGHSCORE,
        elimination_mode=EliminationMode.NONE,
        participant_limit=48,
    )
    accept_r2 = tournament_request_service.accept_request(r2.id, admin.id)
    assert accept_r2.is_ok(), accept_r2.unwrap_err()
    r2 = accept_r2.unwrap()[0]

    r3 = _submit(
        party,
        proposer.id,
        name='Conformance Program Cup',
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        participant_limit=16,
    )
    accept_r3 = tournament_request_service.accept_request(r3.id, admin.id)
    assert accept_r3.is_ok(), accept_r3.unwrap_err()
    r3 = accept_r3.unwrap()[0]
    create_result = tournament_service.create_tournament(
        party.id,
        r3.name,
        game=r3.game,
        contestant_type=ContestantType.SOLO,
        game_format=r3.game_format,
        elimination_mode=r3.elimination_mode,
        max_players=r3.participant_limit,
        start_time=r3.preferred_start_time,
        created_from_request_id=r3.id,
        initiator_id=admin.id,
    )
    assert create_result.is_ok(), create_result.unwrap_err()
    tournament, _event = create_result.unwrap()
    appoint_result = tournament_request_service.appoint_proposer_orga(
        tournament.id, proposer.id, admin.id
    )
    assert appoint_result.is_ok(), appoint_result.unwrap_err()

    r4 = _submit(party, proposer.id, name='Conformance Rejected Cup')
    reject_r4 = tournament_request_service.reject_request(
        r4.id, admin.id, _R4_REJECT_REASON
    )
    assert reject_r4.is_ok(), reject_r4.unwrap_err()
    r4 = reject_r4.unwrap()

    r5 = _submit(party, proposer.id, name='Conformance Withdrawn Cup')
    withdraw_r5 = tournament_request_service.withdraw_request(
        r5.id, proposer.id
    )
    assert withdraw_r5.is_ok(), withdraw_r5.unwrap_err()
    r5 = withdraw_r5.unwrap()

    return {
        'r1': r1,
        'r2': r2,
        'r3': r3,
        'tournament': tournament,
        'r4': r4,
        'r5': r5,
    }


@pytest.fixture(scope='module')
def browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser_ = playwright.chromium.launch(executable_path=CHROMIUM_PATH)
        yield browser_
        browser_.close()


# Serve same-origin requests through the Flask test client; log them.

_SITE_FILE_RE = re.compile(r'^/static_sites/([^/]+)/(.+)$')

_PASSTHROUGH_REQUEST_HEADERS = {
    'host',
    'content-length',
    'accept-encoding',
    'connection',
}
_PASSTHROUGH_RESPONSE_HEADERS = {
    'content-encoding',
    'content-length',
    'transfer-encoding',
    'connection',
}

# Resource types that must get a 2xx/304 answer with a Content-Type.
_THEME_RESOURCE_TYPES = {'stylesheet', 'script', 'font'}


def _serve_site_file(route, path: str, record: dict[str, Any]) -> bool:
    match = _SITE_FILE_RE.match(path)
    if not match:
        return False
    site_id, rel_path = match.groups()
    file_path = REPO_ROOT / 'sites' / site_id / 'static' / rel_path
    if file_path.is_file():
        content_type = (
            mimetypes.guess_type(str(file_path))[0]
            or 'application/octet-stream'
        )
        record['answered_via'] = 'disk'
        record['status'] = 200
        record['content_type'] = content_type
        route.fulfill(
            status=200, content_type=content_type, body=file_path.read_bytes()
        )
    else:
        record['answered_via'] = 'disk'
        record['status'] = 404
        record['content_type'] = None
        route.fulfill(status=404, body=b'')
    return True


def _make_route_handler(
    client,
    overrides: dict[str, tuple[str, Any]],
    origin_netloc: str,
    resource_log: list[dict[str, Any]],
):
    def handle(route, request):
        parsed = urlsplit(request.url)
        same_origin = parsed.netloc == origin_netloc
        record: dict[str, Any] = {
            'url': request.url,
            'resource_type': request.resource_type,
            'same_origin': same_origin,
        }
        resource_log.append(record)

        if not same_origin:
            record['answered_via'] = 'aborted (cross-origin)'
            record['status'] = None
            record['content_type'] = None
            route.abort()
            return

        if _serve_site_file(route, parsed.path, record):
            return

        path = parsed.path
        method = request.method
        data: Any = request.post_data

        if request.is_navigation_request() and path in overrides:
            method, data = overrides.pop(path)

        full_path = path + (f'?{parsed.query}' if parsed.query else '')
        headers = {
            key: value
            for key, value in request.headers.items()
            if key.lower() not in _PASSTHROUGH_REQUEST_HEADERS
        }
        response = client.open(
            full_path, method=method, data=data, headers=headers
        )
        response_headers = {
            key: value
            for key, value in response.headers.items()
            if key.lower() not in _PASSTHROUGH_RESPONSE_HEADERS
        }
        record['answered_via'] = 'flask'
        record['status'] = response.status_code
        record['content_type'] = response.headers.get('Content-Type')
        route.fulfill(
            status=response.status_code,
            headers=response_headers,
            body=response.get_data(),
        )

    return handle


def _check_resource_log(
    frame_key: str, resource_log: list[dict[str, Any]]
) -> list[str]:
    """Fail loudly on a broken same-origin stylesheet/script/font."""
    problems: list[str] = []
    cross_origin_theme_needs: list[str] = []

    for record in resource_log:
        rtype = record['resource_type']
        if rtype not in _THEME_RESOURCE_TYPES:
            continue

        if not record['same_origin']:
            cross_origin_theme_needs.append(f'{rtype} {record["url"]}')
            continue

        status = record['status']
        status_ok = status is not None and (
            status == 304 or 200 <= status < 300
        )
        if not status_ok:
            problems.append(
                f'{frame_key}: {rtype} {record["url"]} answered via '
                f'{record["answered_via"]} -> status {status}'
            )
            continue

        if rtype == 'stylesheet':
            content_type = (record['content_type'] or '').split(';')[0].strip()
            if content_type.lower() != 'text/css':
                problems.append(
                    f'{frame_key}: stylesheet {record["url"]} answered via '
                    f'{record["answered_via"]} -> Content-Type '
                    f'{record["content_type"]!r} (expected text/css)'
                )

    if cross_origin_theme_needs:
        problems.append(
            f'{frame_key}: theme depends on cross-origin resource(s) the '
            'harness aborted: ' + ', '.join(cross_origin_theme_needs)
        )

    return problems


_A3_POST_DATA = {
    'name': '   ',
    'game': 'Some Game',
    'game_format': GameFormat.ONE_V_ONE.value,
    'elimination_mode': EliminationMode.SINGLE_ELIMINATION.value,
    'team_size': '1',
    'participant_limit': '1',
    'preferred_start_time': '2026-10-03T14:00',
    'preferred_end_time': '2026-10-03T12:00',
    'description': 'Some description.',
}


def _check_radio_via_label(page, name: str, value: str) -> None:
    # Radios are too small to click; click the wrapping `<label>` instead.
    page.click(f'label:has(input[name="{name}"][value="{value}"])')


def _fill_r1_like(page) -> None:
    page.fill('#name', 'Rollator-Rallye 2026')
    page.fill('#game', 'Mario Kart 8 Deluxe')
    _check_radio_via_label(page, 'game_format', 'FREE_FOR_ALL')
    _check_radio_via_label(page, 'elimination_mode', 'SINGLE_ELIMINATION')
    page.fill('#team_size', '1')
    page.fill('#participant_limit', '32')
    page.fill('#preferred_start_time', '2026-10-03T14:00')
    page.fill('#preferred_end_time', '2026-10-03T20:00')
    page.fill(
        '#description',
        'Drei Cups, gemischte Strecken, Items an. Wer zuerst die Ziellinie '
        'sieht, bekommt den Ehrenkeks.',
    )
    page.fill(
        '#special_rules',
        'Blaue Panzer sind verboten. Wer drängelt, fährt die nächste Runde '
        'rückwärts.',
    )
    page.fill('#request_notes', 'Nur die Orga sieht das.')


_TOKEN_SANITY_JS = r"""
() => ['--paper', '--ink', '--body'].map(
  (name) => getComputedStyle(document.documentElement)
    .getPropertyValue(name)
    .trim()
)
"""
_SANITY_TOKEN_NAMES = ('--paper', '--ink', '--body')


@contextmanager
def _open_page(
    browser,
    *,
    frame_key: str,
    is_site: bool,
    app: BycepsApp,
    user_id,
    viewport: dict[str, int],
    dark: bool,
    path: str,
    method: str = 'GET',
    data: Any = None,
    fill: Any = None,
    click: str | None = None,
) -> Iterator[Any]:
    log_in_user(user_id)
    with http_client(app, user_id=user_id) as client:
        overrides = {path: (method, data)} if method != 'GET' else {}
        resource_log: list[dict[str, Any]] = []
        server_name = app.config.get('SERVER_NAME') or ADMIN_SERVER_NAME
        handler = _make_route_handler(
            client, overrides, server_name, resource_log
        )
        page = browser.new_page(viewport=viewport)
        try:
            page.route('**/*', handler)
            page.goto(f'http://{server_name}{path}')
            page.evaluate('() => document.fonts.ready')

            problems = _check_resource_log(frame_key, resource_log)
            if is_site:
                token_values = page.evaluate(_TOKEN_SANITY_JS)
                for name, value in zip(
                    _SANITY_TOKEN_NAMES, token_values, strict=True
                ):
                    if not value:
                        problems.append(
                            f'{frame_key}: theme token {name} resolved '
                            'empty on document.documentElement (theme '
                            'stylesheet not applied?)'
                        )
            if problems:
                raise AssertionError('\n'.join(problems))

            if dark:
                page.evaluate(
                    "() => { document.documentElement.dataset.theme = 'dark'; }"
                )
            if fill is not None:
                fill(page)
            if click:
                page.click(click)
            yield page
        finally:
            page.close()


def _page_context(
    frame_key: str,
    frame: dict[str, Any],
    *,
    browser,
    conformance_site_app: BycepsApp,
    admin_app: BycepsApp,
    admin: User,
    proposer: User,
    ticketless_user: User,
    requests_: dict[str, Any],
):
    viewport = {'width': frame['viewport'][0], 'height': frame['viewport'][1]}
    dark = frame['theme'] == 'dark'
    page_key = frame['page']

    if frame['app'] == 'site':
        app = conformance_site_app
        r = requests_

        if page_key == 'propose':
            return _open_page(
                browser,
                frame_key=frame_key,
                is_site=True,
                app=app,
                user_id=proposer.id,
                viewport=viewport,
                dark=dark,
                path='/lan-tournaments/requests/propose',
                fill=_fill_r1_like,
            )
        if page_key == 'propose_errors':
            return _open_page(
                browser,
                frame_key=frame_key,
                is_site=True,
                app=app,
                user_id=proposer.id,
                viewport=viewport,
                dark=dark,
                path='/lan-tournaments/requests/propose',
                method='POST',
                data=_A3_POST_DATA,
            )
        if page_key == 'edit':
            return _open_page(
                browser,
                frame_key=frame_key,
                is_site=True,
                app=app,
                user_id=proposer.id,
                viewport=viewport,
                dark=dark,
                path=f'/lan-tournaments/requests/{r["r1"].id}/update',
            )
        if page_key == 'frozen':
            return _open_page(
                browser,
                frame_key=frame_key,
                is_site=True,
                app=app,
                user_id=proposer.id,
                viewport=viewport,
                dark=dark,
                path=f'/lan-tournaments/requests/{r["r2"].id}/update',
            )
        if page_key == 'dash':
            return _open_page(
                browser,
                frame_key=frame_key,
                is_site=True,
                app=app,
                user_id=proposer.id,
                viewport=viewport,
                dark=dark,
                path='/lan-tournaments/requests',
            )
        if page_key == 'empty':
            return _open_page(
                browser,
                frame_key=frame_key,
                is_site=True,
                app=app,
                user_id=ticketless_user.id,
                viewport=viewport,
                dark=dark,
                path='/lan-tournaments/requests',
            )
        raise AssertionError(f'{frame_key}: unknown site page key {page_key!r}')

    app = admin_app
    r = requests_
    if page_key == 'queue':
        return _open_page(
            browser,
            frame_key=frame_key,
            is_site=False,
            app=app,
            user_id=admin.id,
            viewport=viewport,
            dark=dark,
            path=f'/lan-tournaments/for_party/{r["r1"].party_id}/requests',
        )
    if page_key == 'detail_open':
        return _open_page(
            browser,
            frame_key=frame_key,
            is_site=False,
            app=app,
            user_id=admin.id,
            viewport=viewport,
            dark=dark,
            path=f'/lan-tournaments/requests/{r["r1"].id}',
        )
    if page_key == 'detail_accepted':
        return _open_page(
            browser,
            frame_key=frame_key,
            is_site=False,
            app=app,
            user_id=admin.id,
            viewport=viewport,
            dark=dark,
            path=f'/lan-tournaments/requests/{r["r2"].id}',
        )
    if page_key == 'detail_390':
        return _open_page(
            browser,
            frame_key=frame_key,
            is_site=False,
            app=app,
            user_id=admin.id,
            viewport=viewport,
            dark=dark,
            path=f'/lan-tournaments/requests/{r["r1"].id}',
            click='details.rej > summary',
        )
    if page_key == 'edit_admin':
        return _open_page(
            browser,
            frame_key=frame_key,
            is_site=False,
            app=app,
            user_id=admin.id,
            viewport=viewport,
            dark=dark,
            path=f'/lan-tournaments/requests/{r["r1"].id}/update',
        )
    raise AssertionError(f'{frame_key}: unknown admin page key {page_key!r}')


@pytest.mark.skipif(not _GATE_OPEN, reason=_SKIP_REASON)
@pytest.mark.parametrize('frame_key', FRAME_KEYS)
def test_frame_matches_draft(
    frame_key,
    browser,
    conformance_site_app,
    conformance_admin_app,
    admin,
    proposer,
    ticketless_user,
    requests_,
):
    frame = FRAMES[frame_key]
    golden_frame = _GOLDEN.get(frame_key, {})
    frame_pairs = PAIRS[frame_key]
    ours_scope = frame['ours_scope']

    deviations: list[str] = []

    with _page_context(
        frame_key,
        frame,
        browser=browser,
        conformance_site_app=conformance_site_app,
        admin_app=conformance_admin_app,
        admin=admin,
        proposer=proposer,
        ticketless_user=ticketless_user,
        requests_=requests_,
    ) as page:
        for label, _draft_sel, ours_sel, opts in frame_pairs:
            golden_props = golden_frame.get(label)
            if not golden_props:
                # Nothing declared on the draft side: no signal to compare.
                continue

            allow = opts.get('allow', {})
            prop_specs = [
                [prop, info['relative']]
                for prop, info in golden_props.items()
                if prop not in allow
            ]
            if not prop_specs:
                continue

            result = page.evaluate(
                _OURS_EXTRACT_JS, [ours_scope, ours_sel, prop_specs]
            )
            if result is None:
                deviations.append(f'{frame_key} · {label} · MISSING')
                continue

            for prop, relative in prop_specs:
                golden_value = golden_props[prop]['computed']
                ours_value = result.get(prop)
                if ours_value is None:
                    deviations.append(
                        f'{frame_key} · {label} · {prop} · MISSING'
                    )
                    continue
                if not _values_match(golden_value, ours_value, relative, prop):
                    deviations.append(
                        f'{frame_key} · {label} · {prop} · '
                        f'{golden_value} → {ours_value}'
                    )

    assert not deviations, '\n'.join(deviations)
