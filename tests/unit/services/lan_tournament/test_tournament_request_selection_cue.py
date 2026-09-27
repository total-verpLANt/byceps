"""
tests.unit.services.lan_tournament.test_tournament_request_selection_cue
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Static guards for workspace-dim0.19: the game-format/elimination-mode
selection cue must follow `:checked` (CSS-first, so it also works
without JS) on both the generic base theme and the totalverplant-36
"bote" override, with a visible keyboard focus ring on the hidden
format radios. The bote style partial must still carry no Jinja-looking
tokens -- the trap this issue's briefing calls out by name.
"""

import pathlib
import re


_CSS_PATH = pathlib.Path('byceps/static/style/lan_tournament.css')
_BOTE_STYLE_PARTIAL = pathlib.Path(
    'sites/totalverplant-36/template_overrides/site/lan_tournament'
    '/_bote_request_style.html'
)
_JS_PATH = pathlib.Path('byceps/static/behavior/lan_tournament_request.js')

_HEX_RE = re.compile(r'#[0-9a-fA-F]{3,8}')


def _rule(src: str, selector: str) -> str:
    """Return one rule's full text, from `selector` to its closing
    `}` (inclusive). `selector` must be the last, comma-joined
    selector directly before the declaration block, so the first `}`
    found after it is that rule's own closing brace."""
    start = src.index(selector)
    end = src.index('}', start)
    return src[start : end + 1]


# ------------------------------------------------------------------ #
# lan_tournament.css (.lt-dark theme)
# ------------------------------------------------------------------ #


def test_lt_dark_seg_option_follows_checked_state():
    """A user click on a format radio must move the highlight even
    without the JS auto-reselect path touching a class."""
    src = _CSS_PATH.read_text()

    assert '.lt-dark .seg-option:has(input:checked)' in src
    # The server-rendered class rule stays, for the no-JS/no-:has() case.
    assert '.lt-dark .seg-option.is-selected' in src


def test_lt_dark_opt_follows_checked_state():
    """A manually clicked mode card must not keep the old `.on`/
    `.is-selected` highlight once a different mode is checked."""
    src = _CSS_PATH.read_text()

    assert '.lt-dark .opt:has(input:checked)' in src
    assert '.lt-dark .opt.is-selected' in src


def test_lt_dark_seg_option_has_visible_focus_ring():
    """The format radio is `opacity: 0`; Tab must still show a
    visible cue on its label card."""
    src = _CSS_PATH.read_text()

    assert '.lt-dark .seg-option:has(input:focus-visible)' in src
    rule = _rule(src, '.lt-dark .seg-option:has(input:focus-visible)')
    assert 'outline' in rule


def test_lt_dark_new_rules_use_lt_tokens_not_hex():
    """Never hard-code a hex value; use the theme's own `--lt-*`
    custom properties."""
    src = _CSS_PATH.read_text()

    for selector in (
        '.lt-dark .seg-option:has(input:checked)',
        '.lt-dark .seg-option:has(input:focus-visible)',
        '.lt-dark .opt:has(input:checked)',
    ):
        rule = _rule(src, selector)
        assert not _HEX_RE.search(rule), f'hard-coded hex in {selector!r}'
        assert '--lt-' in rule, f'no --lt- token in {selector!r}'


# ------------------------------------------------------------------ #
# totalverplant-36 bote override
# ------------------------------------------------------------------ #


def test_bote_seg_option_follows_checked_state():
    src = _BOTE_STYLE_PARTIAL.read_text()

    assert '.request-page .seg-option:has(input:checked)' in src
    assert '.request-page .seg-option.on' in src


def test_bote_opt_follows_checked_state():
    src = _BOTE_STYLE_PARTIAL.read_text()

    assert '.request-page .opt:has(input:checked)' in src
    assert '.request-page .opt.on' in src


def test_bote_seg_option_has_visible_focus_ring():
    src = _BOTE_STYLE_PARTIAL.read_text()

    assert '.request-page .seg-option:has(input:focus-visible)' in src
    rule = _rule(src, '.request-page .seg-option:has(input:focus-visible)')
    assert 'outline' in rule


def test_bote_new_rules_use_theme_tokens_not_hex():
    src = _BOTE_STYLE_PARTIAL.read_text()

    for selector in (
        '.request-page .seg-option:has(input:checked)',
        '.request-page .seg-option:has(input:focus-visible)',
        '.request-page .opt:has(input:checked)',
    ):
        rule = _rule(src, selector)
        assert not _HEX_RE.search(rule), f'hard-coded hex in {selector!r}'
        assert 'var(--' in rule, f'no theme token in {selector!r}'


def test_bote_style_partial_has_no_jinja_looking_tokens():
    """A `{% %}` / `{{ }}` / `{# #}` sequence inside this included
    partial's CSS would parse and 500 the page -- this has broken the
    codebase before. Assert none of the six token halves appear
    anywhere in the file."""
    src = _BOTE_STYLE_PARTIAL.read_text()

    for token in ('{%', '%}', '{{', '}}', '{#', '#}'):
        assert token not in src, f'found Jinja-looking token {token!r}'


# ------------------------------------------------------------------ #
# lan_tournament_request.js
# ------------------------------------------------------------------ #


def test_js_syncs_selection_class_on_manual_game_format_change():
    """A manual click on a format radio moves `is-selected`/`on` off
    the previously-checked card and onto the newly-checked one."""
    src = _JS_PATH.read_text()

    assert "syncSelectedCard(target.form, 'game_format', '.seg-option')" in src


def test_js_syncs_selection_class_on_manual_elimination_mode_change():
    """Same guarantee for a manually clicked mode card, not just the
    JS auto-reselect path."""
    src = _JS_PATH.read_text()

    assert "syncSelectedCard(target.form, 'elimination_mode', '.opt')" in src


def test_js_has_no_html_sink_or_eval_calls():
    src = _JS_PATH.read_text()

    assert not re.search(r'innerHTML|outerHTML|insertAdjacentHTML|eval\(', src)


def test_js_has_no_user_facing_string_literals():
    src = _JS_PATH.read_text()

    assert not re.search(
        r'Teilnehmer|Team ?limit|Participant'
        r'|Highscore|Only available|Not available',
        src,
    )
