"""Keep provenance server-side without losing documentation or changing CSS."""

from hashlib import sha256
from pathlib import Path
import re

from jinja2 import Environment, FileSystemLoader, StrictUndefined
import pytest


_BOTE_DIR = Path(
    'sites/totalverplant-36/template_overrides/site/lan_tournament'
)
_INTERNAL_PATH = re.compile(r'[\w./-]+\.(html|css)\b')
_JINJA_COMMENT = re.compile(r'{#(.*?)#}', re.DOTALL)
_STYLE = re.compile(r'<style>(.*?)</style>', re.DOTALL)

# Original comment bodies and remaining source, including CSS section comments.
# These snapshots prevent "fixing" a leak by deleting its documentation or rules.
_PRESERVED = {
    '_bote_qualification_style.html': (
        ['9da4dd959f47e0362dce39bc4e9d228987d449ed845c844d93362cd328ae239c'],
        'd3bb42df24abcc2ceaba412735046883ab750f988e1efb6a300bd9fb9f3f5d71',
    ),
    '_bote_seeding_style.html': (
        ['ad85b9e9e3ef465e21234e45c3fe9b3e5eae511181480613a694f215913562d0'],
        'fd2e295d4b4f00bac850b7664e4d5c51d2ca838ea49bb071ca4610d91f939dcc',
    ),
    '_bote_tournament_style.html': (
        [
            '0a15f1a0f42d27231b315959716465477dfa43d92b56e1aa27e03eb16d60001b',
            '676a2c5b795afe79028e5d5b02a6bd58d7a36c567c6c5868a7415b6da2f23112',
        ],
        'b40e4d52388c8cf96fdbc35dc2be1896271290c11052a60a3bb3a1dcbbb6bec2',
    ),
    '_bote_request_style.html': (
        [],
        '3532e35b1b5effc5255aeb499f97bb3cbb5ae09fdb78cb0e223662d0cf72740c',
    ),
}


def _style_leaks(source: str) -> list[str]:
    styles = _STYLE.findall(source)
    assert styles, 'The style partial must still contain CSS.'
    return [
        match.group(0)
        for style in styles
        for match in _INTERNAL_PATH.finditer(style)
    ]


def test_shipped_style_comments_name_no_internal_paths():
    partials = sorted(_BOTE_DIR.glob('_bote_*.html'))
    assert partials
    leaks = [
        (path.name, leak)
        for path in partials
        for leak in _style_leaks(_JINJA_COMMENT.sub('', path.read_text()))
    ]

    assert not leaks, f'{len(leaks)} shipped internal path matches: {leaks}'


def test_rendered_style_includes_name_no_internal_paths():
    env = Environment(
        loader=FileSystemLoader(_BOTE_DIR),
        undefined=StrictUndefined,
        autoescape=True,
        keep_trailing_newline=True,
    )
    leaks = []
    for path in sorted(_BOTE_DIR.glob('_bote_*.html')):
        rendered = env.from_string('{% include partial %}').render(
            partial=path.name
        )
        leaks.extend((path.name, leak) for leak in _style_leaks(rendered))
        assert rendered == _JINJA_COMMENT.sub('', path.read_text())

    assert not leaks, f'{len(leaks)} rendered internal path matches: {leaks}'


@pytest.mark.parametrize('filename', _PRESERVED)
def test_style_documentation_rules_and_section_comments_are_preserved(filename):
    source = (_BOTE_DIR / filename).read_text()
    comment_hashes, shipped_hash = _PRESERVED[filename]
    bodies = _JINJA_COMMENT.findall(source)

    assert [
        sha256(body.encode()).hexdigest() for body in bodies
    ] == comment_hashes
    assert all('#}' not in body for body in bodies)
    assert (
        sha256(_JINJA_COMMENT.sub('', source).encode()).hexdigest()
        == shipped_hash
    )
    # Jinja delimiters belong outside CSS comments, never inside them.
    assert all(
        '{#' not in body and '#}' not in body
        for body in re.findall(r'/\*(.*?)\*/', source, re.DOTALL)
    )
