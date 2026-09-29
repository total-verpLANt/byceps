"""
tests.unit.services.lan_tournament.test_create_wizard_js_sinks
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Server-supplied URLs reach the wizard's DOM only through `safeUrl`.
"""

from pathlib import Path
import re

import pytest


_BEHAVIOR = Path(__file__).resolve().parents[4] / 'byceps/static/behavior'
_SCRIPTS = [
    _BEHAVIOR / 'lan_tournament_create_wizard.js',
    _BEHAVIOR / 'lan_tournament_create_wizard_image.js',
]

_URL_SINK = re.compile(r"setAttribute\('(?:src|href)',\s*([^;]*)\);")


def _between(source: str, start: str, end: str) -> str:
    begin = source.index(start)
    return source[begin : source.index(end, begin)]


@pytest.mark.parametrize('script', _SCRIPTS, ids=lambda path: path.name)
def test_no_url_attribute_takes_a_raw_url_property(script):
    sinks = _URL_SINK.findall(script.read_text())

    assert sinks
    assert [sink for sink in sinks if '.url' in sink] == []


def test_picker_thumbnail_takes_its_url_through_safe_url():
    source = (_BEHAVIOR / 'lan_tournament_create_wizard_image.js').read_text()
    render_item = _between(
        source, 'function renderItem(item)', 'function parseItems'
    )

    assert "setAttribute('src', item.url)" not in render_item
    thumb = re.search(r'var (\w+) = safeUrl\(item\.url\);', render_item)
    assert thumb is not None
    assert f"setAttribute('src', {thumb.group(1)});" in render_item
