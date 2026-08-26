from pathlib import Path
import re


CSS = (
    Path(__file__).parents[4] / 'byceps' / 'static' / 'style'
) / 'lan_tournament.css'
MATCH_TEMPLATE = (
    Path(__file__).parents[4]
    / 'byceps/services/lan_tournament/blueprints/site/templates'
    / 'site/lan_tournament/view_match.html'
)

GREEN = 'var(--green, var(--lt-brand-success))'
RED_DEEP = 'var(--redDeep, var(--lt-brand-danger))'
LIGHT = 'var(--paperLite, var(--lt-ink))'
MONO = 'var(--mono, monospace)'
MUTED = 'var(--lt-muted)'
INK = 'var(--lt-text)'


def _parse(css, media=None):
    """Return `(media, selector, declarations)` for every style rule."""
    css = re.sub(r'/\*.*?\*/', '', css, flags=re.DOTALL)
    rules = []
    position = 0
    while (start := css.find('{', position)) != -1:
        prelude = ' '.join(css[position:start].split())
        depth, end = 1, start + 1
        while depth:
            depth += {'{': 1, '}': -1}.get(css[end], 0)
            end += 1
        body = css[start + 1 : end - 1]
        position = end
        if prelude.startswith('@media'):
            rules.extend(_parse(body, prelude.removeprefix('@media ')))
        elif not prelude.startswith('@'):
            declarations = {}
            for declaration in body.split(';'):
                if ':' in declaration:
                    name, value = declaration.split(':', 1)
                    declarations[name.strip()] = ' '.join(value.split())
            rules.extend(
                (media, selector.strip(), declarations)
                for selector in prelude.split(',')
            )
    return rules


def _rule(selector, media=None):
    merged = {}
    for rule_media, rule_selector, declarations in _parse(CSS.read_text()):
        if rule_media == media and rule_selector == selector:
            merged.update(declarations)
    assert merged, f'no rule for {selector!r} (media {media!r})'
    return merged


def _declared(selector, name):
    """Return every value declared for `name` in the rule, in source order."""
    css = re.sub(r'/\*.*?\*/', '', CSS.read_text(), flags=re.DOTALL)
    body = re.search(re.escape(selector) + r'\s*\{([^}]*)\}', css).group(1)
    return re.findall(rf'(?:^|;|\s){name}\s*:\s*([^;]+)', body)


def _tracks(template):
    tracks, depth, current = [], 0, ''
    for char in template:
        depth += {'(': 1, ')': -1}.get(char, 0)
        if char == ' ' and not depth:
            tracks.append(current)
            current = ''
        else:
            current += char
    return [*tracks, current]


def test_filter_buttons_are_44px_and_wrap():
    assert _rule('.match-filter-bar')['flex-wrap'] == 'wrap'

    button = _rule('.match-filter-bar .button')
    assert button['min-height'] == '44px'
    assert button['display'] == 'inline-flex'
    assert button['border'] == f'1.5px solid {INK}'
    assert button['background'] == 'var(--lt-panel)'
    assert button['font-family'] == MONO
    assert button['text-transform'] == 'uppercase'
    assert button['letter-spacing']

    focus = _rule('.match-filter-bar .button:focus-visible')
    assert focus['outline'] == f'2px solid {RED_DEEP}'

    count = _rule('.match-filter-bar__count')
    assert count['font-weight'] == '400'
    assert count['color'] == MUTED

    label = _rule('.match-filter-nav__label')
    assert label['font-family'] == MONO
    assert label['text-transform'] == 'uppercase'
    assert label['letter-spacing'] and label['color'] == MUTED


def test_active_filter_and_toggle_states_styled():
    current = _rule('.match-filter-bar .button[aria-current="page"]')
    assert current['background'] == INK
    assert current['color'] == LIGHT

    mine = '.match-filter-bar .button.match-filter-bar__mine'
    assert _rule(mine)['border-style'] == 'dashed'
    pressed = _rule(f'{mine}[aria-pressed="true"]')
    assert pressed['border-style'] == 'solid'
    assert pressed['background'] == RED_DEEP
    assert pressed['border-color'] == RED_DEEP
    assert pressed['color'] == LIGHT
    assert _declared(f'{mine}::before', 'content') == ['"☐ "', '"☐ " / ""']
    assert _declared(f'{mine}[aria-pressed="true"]::before', 'content') == [
        '"☑ "',
        '"☑ " / ""',
    ]

    selectors = {selector for _, selector, _ in _parse(CSS.read_text())}
    assert '.match-filter-bar .button.active' not in selectors


def test_status_badge_variants_defined():
    base = _rule('.readiness-badge')
    assert base['border'] == f'1.5px solid {INK}'
    assert base['font-family'] == MONO
    assert base['text-transform'] == 'uppercase'

    done = _rule('.readiness-badge--done')
    assert done['border-color'] == GREEN and done['color'] == GREEN

    both = _rule('.readiness-badge--both')
    assert both['background'] == GREEN and both['border-color'] == GREEN
    assert both['color'] == LIGHT

    for variant in ('wait', 'ffa'):
        waiting = _rule(f'.readiness-badge--{variant}')
        assert waiting['border-style'] == 'dashed'
        assert waiting['border-color'] == MUTED and waiting['color'] == MUTED

    for variant in ('none', 'part'):
        plain = _rule(f'.readiness-badge--{variant}')
        assert plain['border-color'] == INK and plain['color'] == INK

    detail = _rule('.readiness-badge__detail')
    assert detail['font-style'] == 'italic' and detail['color'] == MUTED

    section = CSS.read_text().split('1h. Match table / match list', 1)[1]
    section = section.split('1i. Match detail page', 1)[0]
    assert not re.search(r'#[0-9a-fA-F]{3,8}\b', section)
    assert 'ui-monospace' not in section and 'Menlo' not in section


def test_list_collapses_at_360px():
    desktop = _tracks(_rule('.match-row')['grid-template-columns'])
    assert len(desktop) == 4
    assert desktop[1].endswith('1fr)')

    head = _rule('.match-row--head')
    assert head['font-family'] == MONO
    assert head['text-transform'] == 'uppercase'
    assert head['letter-spacing'] and head['color'] == MUTED
    assert _rule('.match-list-empty')['font-style'] == 'italic'
    assert _rule('.match-list-empty')['color'] == MUTED

    small = '(max-width: 600px)'
    row = _rule('.match-row', small)
    assert len(_tracks(row['grid-template-columns'])) == 3
    assert _rule('.match-row--head', small)['display'] == 'none'
    status = _rule('.match-row .match-list-status', small)
    assert status['grid-column'] == '1 / -1'

    other_media = {
        media
        for media, selector, _ in _parse(CSS.read_text())
        if selector in {'.match-row', '.match-list-status'}
    }
    assert other_media <= {None, small}

    assert _rule('.readiness-btn', '(max-width: 480px)')['width'] == '100%'


def test_match_board_wraps_long_names():
    box = _rule('.contestants-box')
    assert _tracks(box['grid-template-columns']) == [
        'minmax(0, 1fr)',
        'auto',
        'minmax(0, 1fr)',
    ]
    assert _rule('.contestant')['min-width'] == '0'
    assert _rule('.contestant-name')['overflow-wrap'] == 'anywhere'
    assert _rule('.podium__name')['overflow-wrap'] == 'anywhere'

    template = MATCH_TEMPLATE.read_text()
    for hook in (
        'contestants-box',
        'contestant',
        'contestant-name',
        'podium__name',
    ):
        assert re.search(rf"class=['\"][^'\"]*\b{hook}\b", template), hook
