"""
tests.unit.services.lan_tournament.test_bote_paper_ground_theme
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Static checks for the Papiergrund patch (capture-safe paper ground):
the ground lives on `body` (canvas propagation), never on a fixed
`body::before` layer, so scrolling, full-page capture and print all
paint the same background.
"""

import pathlib
import re


_LIGHT_CSS_PATH = pathlib.Path(
    'sites/totalverplant-36/static/style/totalverplant-36.css'
)
_DARK_CSS_PATH = pathlib.Path(
    'sites/totalverplant-36/static/style/bote-theme.css'
)
_GRAIN_SVG_PATH = pathlib.Path('sites/totalverplant-36/static/style/grain.svg')

_PLAIN_HTML_RULE_RE = re.compile(r'(?<![\w.#\[-])html\s*\{([^}]*)\}')


def _read(path: pathlib.Path) -> str:
    return path.read_text(encoding='utf-8')


def test_no_fixed_ground_layer_in_theme_files():
    light_css = _read(_LIGHT_CSS_PATH)
    dark_css = _read(_DARK_CSS_PATH)

    assert 'body::before' not in light_css
    assert 'body::before' not in dark_css


def test_body_carries_ground_tokens():
    light_css = _read(_LIGHT_CSS_PATH)

    body_blocks = re.findall(r'body\s*\{([^}]*)\}', light_css)
    ground_block = next(block for block in body_blocks if '--paper' in block)

    assert (
        'background-image: var(--groundShade), var(--groundTex);'
        in ground_block
    )
    assert 'background-size: 100% 100%, var(--groundTexSize);' in ground_block


def test_ground_shade_is_horizontal_only():
    light_css = _read(_LIGHT_CSS_PATH)

    ground_shade_start = light_css.index('--groundShade:')
    declaration = light_css[ground_shade_start : ground_shade_start + 200]

    assert declaration.startswith('--groundShade: linear-gradient(to right')
    assert 'radial-gradient(ellipse' not in light_css


def test_html_has_no_background():
    for path in (_LIGHT_CSS_PATH, _DARK_CSS_PATH):
        css = _read(path)
        for block in _PLAIN_HTML_RULE_RE.findall(css):
            assert 'background' not in block, (
                f'plain `html` rule in {path} declares a background'
            )


def test_dark_theme_swaps_ground_tokens():
    dark_css = _read(_DARK_CSS_PATH)

    dark_blocks = re.findall(
        r'html\[data-theme="dark"\]\s*\{([^}]*)\}', dark_css
    )
    ground_block = next(
        block for block in dark_blocks if '--groundShadeInk' in block
    )

    assert '--groundShadeInk: black;' in ground_block
    assert '--groundShadeK: 0.40;' in ground_block


def test_flat_fallback_and_print_rules_present():
    light_css = _read(_LIGHT_CSS_PATH)

    assert 'prefers-reduced-transparency: reduce' in light_css
    assert 'forced-colors: active' in light_css
    assert 'background-image: none;' in light_css
    assert 'print-color-adjust: exact' in light_css


def test_grain_alpha_is_0_275_and_no_metadata():
    grain_svg = _read(_GRAIN_SVG_PATH)
    lowered = grain_svg.lower()

    assert '0 0 0 0.275 0' in grain_svg
    assert 'c2pa' not in lowered
    assert '<metadata' not in lowered


def test_body_child_stacking_rule_kept():
    light_css = _read(_LIGHT_CSS_PATH)

    assert 'body > * { position: relative; z-index: 1; }' in light_css
