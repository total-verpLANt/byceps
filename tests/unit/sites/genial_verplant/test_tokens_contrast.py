"""
tests.unit.sites.genial_verplant.test_tokens_contrast
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

import pytest

from tests.unit.sites.genial_verplant._tokens import (
    contrast,
    find_block,
    load_token_sets,
    parse_blocks,
    strip_comments,
    STYLE_DIR,
)


TOKEN_SETS = load_token_sets()

# fmt: off
TEXT_PAIRS = [
    ('fg', 'bg'),
    ('fg', 's1'),
    ('fg', 's2'),
    ('muted', 'bg'),
    ('muted', 's1'),
    ('muted', 's2'),
    ('accent-text', 'bg'),
    ('accent-text', 's1'),
    ('accent-text', 's2'),
    ('on-accent', 'accent'),
    ('ok', 's1'),
    ('warn', 's1'),
    ('bad', 's1'),
    ('info', 's1'),
]

CONTROL_PAIRS = [
    ('ctl', 'bg'),
    ('ctl', 's1'),
]

# Each `--lt-*` literal in the bridge and the Owly token it must equal.
BRIDGE_TOKEN_TWINS = [
    ('--lt-brand-blue',    '--info'),
    ('--lt-brand-orange',  '--accent'),
    ('--lt-brand-success', '--ok'),
    ('--lt-brand-warning', '--warn'),
    ('--lt-brand-danger',  '--bad'),
    ('--lt-bg',            '--bg'),
    ('--lt-bg-2',          '--s1'),
    ('--lt-panel',         '--s1'),
    ('--lt-card',          '--s2'),
    ('--lt-white',         '--fg'),
    ('--lt-border',        '--line'),
    ('--lt-line',          '--line'),
    ('--lt-line-strong',   '--ctl'),
    ('--lt-text',          '--fg'),
    ('--lt-muted',         '--muted'),
    ('--lt-ink',           '--bg'),
    ('--lt-radius',        '--radius'),
    ('--lt-radius-sm',     '--radius-sm'),
]
# fmt: on

# The module token without an Owly twin: a literal, not a colour.
BRIDGE_UNMAPPED_TOKENS = {'--lt-shadow': 'none'}

BRIDGE_CSS = STYLE_DIR / 'gv-lan-tournament.css'
BRIDGE_SELECTOR = ':root[data-era] body.lt-dark'


def _pair_id(pair):
    return f'{pair[0]}-on-{pair[1]}'


def _ratio(tokens, foreground, background):
    return contrast(tokens[f'--{foreground}'], tokens[f'--{background}'])


def test_token_sets_cover_all_five_looks():
    assert list(TOKEN_SETS) == [
        'owly-dark',
        'owly-light',
        'clan-2003',
        'lan-99',
        'content-well',
    ]


def test_contrast_helper_matches_wcag_reference_values():
    assert contrast('#000000', '#ffffff') == pytest.approx(21.0)
    assert contrast('#ffffff', '#000000') == pytest.approx(21.0)
    assert contrast('#f39200', '#f39200') == pytest.approx(1.0)
    # #767676 is the lightest grey that passes AA on white.
    assert contrast('#767676', '#ffffff') == pytest.approx(4.54, abs=0.01)
    assert contrast('#777777', '#ffffff') < 4.5


@pytest.mark.parametrize('pair', TEXT_PAIRS, ids=_pair_id)
@pytest.mark.parametrize('set_name', TOKEN_SETS)
def test_text_pairs_meet_aa(set_name, pair):
    tokens = TOKEN_SETS[set_name]
    foreground, background = pair

    ratio = _ratio(tokens, foreground, background)

    assert ratio >= 4.5, (
        f'{set_name}: {foreground} {tokens[f"--{foreground}"]} on'
        f' {background} {tokens[f"--{background}"]} is {ratio:.2f}:1'
    )


@pytest.mark.parametrize('pair', CONTROL_PAIRS, ids=_pair_id)
@pytest.mark.parametrize('set_name', TOKEN_SETS)
def test_control_pairs_meet_non_text_aa(set_name, pair):
    tokens = TOKEN_SETS[set_name]
    foreground, background = pair

    ratio = _ratio(tokens, foreground, background)

    assert ratio >= 3.0, (
        f'{set_name}: {foreground} {tokens[f"--{foreground}"]} on'
        f' {background} {tokens[f"--{background}"]} is {ratio:.2f}:1'
    )


def _bridge_declarations():
    blocks = parse_blocks(
        strip_comments(BRIDGE_CSS.read_text(encoding='utf-8'))
    )
    declarations = find_block(blocks, BRIDGE_SELECTOR)
    return {
        name: value
        for name, value in declarations.items()
        if name.startswith('--lt-')
    }


def _normalise(value):
    return ' '.join(value.lower().split())


@pytest.mark.parametrize(('bridge_token', 'owly_token'), BRIDGE_TOKEN_TWINS)
def test_lan_tournament_bridge_literals_match_owly_dark(
    bridge_token, owly_token
):
    bridge = _bridge_declarations()
    owly_dark = TOKEN_SETS['owly-dark']

    assert bridge_token in bridge
    assert _normalise(bridge[bridge_token]) == _normalise(
        owly_dark[owly_token]
    ), f'{bridge_token} must equal the Owly dark {owly_token}'


def test_lan_tournament_bridge_declares_exactly_the_module_tokens():
    declared = _bridge_declarations()

    expected = {token for token, _ in BRIDGE_TOKEN_TWINS} | set(
        BRIDGE_UNMAPPED_TOKENS
    )
    assert set(declared) == expected
    for token, literal in BRIDGE_UNMAPPED_TOKENS.items():
        assert _normalise(declared[token]) == literal
