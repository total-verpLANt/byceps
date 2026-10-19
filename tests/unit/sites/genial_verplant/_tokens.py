"""
tests.unit.sites.genial_verplant._tokens
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from pathlib import Path
import re


SITE_DIR = Path(__file__).resolve().parents[4] / 'sites' / 'genial-verplant'
STYLE_DIR = SITE_DIR / 'static' / 'style'
TOKENS_CSS = STYLE_DIR / 'gv-tokens.css'

OWLY_DARK = ':root'
OWLY_LIGHT = ':root[data-theme="light"]'
CLAN_2003 = ':root[data-era="2"]'
LAN_99 = ':root[data-era="3"]'
CONTENT_WELL = (
    ':root:is([data-era="2"], [data-era="3"])'
    ' body:not(.page-home) .content-wrapper'
)

_BLOCK = re.compile(r'([^{}]+)\{([^{}]*)\}')
_HEX_COLOUR = re.compile(r'#(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{3})')


def strip_comments(css: str) -> str:
    """Blank out comments, keeping every newline so offsets still map to lines."""
    return re.sub(
        r'/\*.*?\*/',
        lambda match: re.sub(r'[^\n]', ' ', match.group()),
        css,
        flags=re.DOTALL,
    )


def _split_top_level(text: str, separator: str) -> list[str]:
    """Split on `separator`, ignoring any inside brackets or quotes."""
    parts = []
    depth = 0
    quote = None
    start = 0
    for index, char in enumerate(text):
        if quote:
            if char == quote:
                quote = None
        elif char in '"\'':
            quote = char
        elif char in '([':
            depth += 1
        elif char in ')]':
            depth -= 1
        elif char == separator and depth == 0:
            parts.append(text[start:index])
            start = index + 1
    parts.append(text[start:])
    return parts


def split_selectors(selector: str) -> list[str]:
    """Return the comma-separated alternatives of a selector list."""
    return [' '.join(part.split()) for part in _split_top_level(selector, ',')]


def parse_declarations(body: str) -> dict[str, str]:
    """Return the `property: value` pairs of a block body."""
    declarations = {}
    for part in _split_top_level(body, ';'):
        name, colon, value = part.partition(':')
        if colon and name.strip():
            declarations[name.strip()] = value.strip()
    return declarations


def parse_blocks(css: str) -> list[tuple[str, dict[str, str], int]]:
    """Return `(selector, declarations, body_offset)` for each rule block.

    Comments must be stripped first. Nested rules (`@media`) are returned
    as their own innermost blocks.
    """
    return [
        (
            ' '.join(match.group(1).split()),
            parse_declarations(match.group(2)),
            match.start(2),
        )
        for match in _BLOCK.finditer(css)
    ]


def find_block(
    blocks: list[tuple[str, dict[str, str], int]], selector: str
) -> dict[str, str]:
    """Merge the declarations of every block that lists `selector`."""
    merged: dict[str, str] = {}
    found = False
    for block_selector, declarations, _ in blocks:
        if selector in split_selectors(block_selector):
            merged.update(declarations)
            found = True
    if not found:
        raise LookupError(f'no CSS block for selector {selector!r}')
    return merged


def load_token_sets(path: Path = TOKENS_CSS) -> dict[str, dict[str, str]]:
    """Return the custom properties of each look, inheriting from `:root`."""
    blocks = parse_blocks(strip_comments(path.read_text(encoding='utf-8')))
    base = find_block(blocks, OWLY_DARK)
    return {
        'owly-dark': base,
        'owly-light': {**base, **find_block(blocks, OWLY_LIGHT)},
        'clan-2003': {**base, **find_block(blocks, CLAN_2003)},
        'lan-99': {**base, **find_block(blocks, LAN_99)},
        'content-well': {**base, **find_block(blocks, CONTENT_WELL)},
    }


def hex_to_rgb(value: str) -> tuple[int, int, int]:
    """Convert `#rgb` or `#rrggbb` to 8-bit channels."""
    value = value.strip()
    if not _HEX_COLOUR.fullmatch(value):
        raise ValueError(f'not a hex colour: {value!r}')
    digits = value[1:]
    if len(digits) == 3:
        digits = ''.join(char * 2 for char in digits)
    return int(digits[0:2], 16), int(digits[2:4], 16), int(digits[4:6], 16)


def relative_luminance(value: str) -> float:
    """Return the WCAG 2.x relative luminance of a hex colour."""

    def linear(channel: int) -> float:
        c = channel / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (linear(channel) for channel in hex_to_rgb(value))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(first: str, second: str) -> float:
    """Return the WCAG 2.x contrast ratio of two hex colours."""
    lighter, darker = sorted(
        (relative_luminance(first), relative_luminance(second)), reverse=True
    )
    return (lighter + 0.05) / (darker + 0.05)
