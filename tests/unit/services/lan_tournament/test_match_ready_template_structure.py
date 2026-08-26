"""Structural checks for the F-04 readiness UI (PRD §26 mobile core case).

The Playwright harness (``playwright_tests/``) requires a live staging
environment and is not runnable in CI sandboxes; these tests verify the
responsive template structure instead: machine-readable DOM data
attributes, 44 px touch targets, and single-source display derivation
wired into both view layers.
"""

import re
from pathlib import Path

MODULE_DIR = (
    Path(__file__).parents[4] / 'byceps' / 'services' / 'lan_tournament'
)

PARTIAL = (
    MODULE_DIR
    / 'blueprints'
    / 'site'
    / 'templates'
    / 'site'
    / 'lan_tournament'
    / '_match_readiness.html'
)
VIEW_MATCH = (
    MODULE_DIR
    / 'blueprints'
    / 'site'
    / 'templates'
    / 'site'
    / 'lan_tournament'
    / 'view_match.html'
)
CSS = MODULE_DIR.parents[1] / 'static' / 'style' / 'lan_tournament.css'

_CONDITION_TAG = re.compile(
    r'\{%-?\s*(if|elif|else|endif)\b(.*?)-?%\}', re.DOTALL
)


def _enclosing_conditions(template: str, position: int) -> list[str]:
    """Return the `if` conditions that wrap `position`, outermost first."""
    open_conditions: list[str] = []
    for tag in _CONDITION_TAG.finditer(template, 0, position):
        keyword, condition = tag.group(1), tag.group(2).strip()
        if keyword == 'if':
            open_conditions.append(condition)
        elif keyword == 'elif':
            open_conditions[-1] = condition
        elif keyword == 'else':
            open_conditions[-1] = f'not ({open_conditions[-1]})'
        else:
            open_conditions.pop()
    return open_conditions


def test_partial_exposes_machine_readable_timestamps():
    """F-03 traffic light consumes ready_at — timestamps must be in the
    DOM as data attributes, not only human-readable text."""
    html = PARTIAL.read_text()

    for attr in (
        'data-match-id',
        'data-readiness-status',
        'data-original-occupied-since',
        'data-pairing-started-at',
        'data-side-a-ready-at',
        'data-side-b-ready-at',
    ):
        assert attr in html, f'missing {attr}'

    # The two occupancy timestamps stay bound to their own projection field.
    for attr, projection_field in (
        ('data-original-occupied-since', 'original_occupied_since'),
        ('data-pairing-started-at', 'pairing_started_at'),
    ):
        assert re.search(
            rf'{attr}="\{{\{{\s*readiness\.{projection_field}\b', html
        ), f'{attr} is not bound to readiness.{projection_field}'

    # Renamed: one attribute per occupancy, no ambiguous legacy name.
    assert 'data-occupied-since' not in html


def test_partial_covers_all_four_display_states():
    html = PARTIAL.read_text()
    for state in (
        'not_yet_occupied',
        "'open'",
        'partially_ready',
        'both_ready',
    ):
        assert state in html


def test_partial_renders_controls_as_forms_with_44px_targets():
    """Controls must work without JS and have ≥44px touch targets."""
    html = PARTIAL.read_text()
    assert 'method="post"' in html or "method='post'" in html
    assert 'readiness-btn' in html

    css = CSS.read_text()
    match = re.search(r'\.readiness-btn\s*\{[^}]*min-height:\s*44px', css)
    assert match, '.readiness-btn lacks min-height: 44px touch target'


def test_site_match_view_includes_readiness_card_for_every_match():
    """Confirmed, finished and FFA matches get the read-only card too."""
    html = VIEW_MATCH.read_text()
    include = "{% include 'site/lan_tournament/_match_readiness.html' %}"
    assert html.count(include) == 1
    assert _enclosing_conditions(html, html.index(include)) == []


def test_partial_withholds_forms_from_read_only_cards():
    """Controls need a mutable match, not only an enabled page context.

    A confirmed or finished match has no `mutation_available`, so the
    card renders its status and timestamps without any form.
    """
    html = PARTIAL.read_text()

    assert re.search(
        r'set can_change = readiness_controls_enabled\s+'
        r'and readiness\.mutation_available\s+'
        r"and tournament\.tournament_status\.name == 'ONGOING'",
        html,
    )

    forms = [form.start() for form in re.finditer(r'<form\b', html)]
    assert forms
    for position in forms:
        assert 'can_change and declared_side in readiness_sides' in (
            _enclosing_conditions(html, position)
        )

    # A confirmed match reads as finished, with no claim or revoke control.
    confirmed = html.index("{{ _('Confirmed') }}")
    assert _enclosing_conditions(html, confirmed) == [
        "readiness.outcome == 'confirmed'"
    ]
    finished = html.index('This match is finished; readiness is read-only.')
    assert _enclosing_conditions(html, finished) == ['readiness.outcome']


def test_both_view_layers_use_shared_derivation():
    site_views = (MODULE_DIR / 'blueprints' / 'site' / 'views.py').read_text()
    admin_views = (MODULE_DIR / 'blueprints' / 'admin' / 'views.py').read_text()
    helpers = (MODULE_DIR / 'lan_tournament_view_helpers.py').read_text()

    for views in (site_views, admin_views):
        assert re.search(
            r'from byceps\.services\.lan_tournament\.lan_tournament_view_helpers'
            r' import \([^)]*\bbuild_match_readiness_projections,',
            views,
        )
        assert 'build_match_readiness_projections(' in views
        # The per-match facade is no longer a view entry point.
        assert 'get_match_readiness(' not in views

    # The shared helper is the single place that derives it.
    assert 'derive_match_readiness(' in helpers
