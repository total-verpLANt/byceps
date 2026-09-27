"""
tests.unit.services.lan_tournament.test_tournament_request_no_internal_refs
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Standing project rule: never ship build-tool names, source paths, or
internal issue/compromise ids in CSS/JS/HTML sent to browsers. Guard the
F-17 (tournament-request) assets only -- earlier sections of
`lan_tournament.css` are tracked separately (workspace-zaw) and are not
this file's concern.
"""

import pathlib
import re


_FORBIDDEN = ('.py', '.html', 'Compromise', 'guardrail', 'workspace-')

_BOTE_STYLE_PARTIAL = pathlib.Path(
    'sites/totalverplant-36/template_overrides/site/lan_tournament'
    '/_bote_request_style.html'
)
_CSS_PATH = pathlib.Path('byceps/static/style/lan_tournament.css')
_JS_PATH = pathlib.Path('byceps/static/behavior/lan_tournament_request.js')

_CSS_F17_SECTION_MARKER = '16. Tournament requests (site)'

_F17_TEMPLATES = [
    pathlib.Path(
        'byceps/services/lan_tournament/blueprints/site/templates'
        '/site/lan_tournament/propose_form.html'
    ),
    pathlib.Path(
        'byceps/services/lan_tournament/blueprints/site/templates'
        '/site/lan_tournament/my_requests.html'
    ),
    pathlib.Path(
        'byceps/services/lan_tournament/blueprints/site/templates'
        '/site/lan_tournament/index.html'
    ),
    pathlib.Path(
        'sites/totalverplant-36/template_overrides/site/lan_tournament'
        '/propose_form.html'
    ),
    pathlib.Path(
        'sites/totalverplant-36/template_overrides/site/lan_tournament'
        '/my_requests.html'
    ),
    pathlib.Path(
        'sites/totalverplant-36/template_overrides/site/lan_tournament'
        '/index.html'
    ),
]

_HTML_COMMENT_RE = re.compile(r'<!--.*?-->', re.DOTALL)


def _assert_no_forbidden_substrings(text: str, *, source: str) -> None:
    for token in _FORBIDDEN:
        assert token not in text, (
            f'forbidden substring {token!r} found in {source}'
        )


def test_bote_request_style_partial_ships_no_internal_references():
    """The whole partial is inlined into a `<style>` block and shipped
    verbatim to the browser -- no template/service/file names or
    internal ids may appear anywhere in it."""
    src = _BOTE_STYLE_PARTIAL.read_text()

    _assert_no_forbidden_substrings(src, source=str(_BOTE_STYLE_PARTIAL))


def test_css_f17_section_ships_no_internal_references():
    """Static CSS can't use Jinja comments, so its own comments must
    already be free of source/template/service names and internal ids.
    Scoped to the F-17 section only (from its own section marker to
    end of file); earlier sections are out of scope here."""
    src = _CSS_PATH.read_text()
    start = src.index(_CSS_F17_SECTION_MARKER)
    f17_section = src[start:]

    _assert_no_forbidden_substrings(
        f17_section, source=f'{_CSS_PATH} (F-17 section)'
    )


def test_js_ships_no_internal_references():
    """`lan_tournament_request.js` is new in F-17; its comments must
    carry no source/template/service names or internal ids."""
    src = _JS_PATH.read_text()

    _assert_no_forbidden_substrings(src, source=str(_JS_PATH))


def test_f17_site_templates_have_no_html_comments_with_internal_references():
    """`{% %}`/`{{ }}` Jinja tags (e.g. `{% extends %}`, `{% include %}`)
    legitimately name other template paths and never reach the
    browser, so they are not checked here. A literal `<!-- -->` HTML
    comment, however, ships to the browser as-is; none of the F-17
    site templates (base or bote override) may carry one that leaks an
    internal reference. Currently none of these templates carry any
    HTML comment at all -- this also guards against one being added
    later with such a leak."""
    for path in _F17_TEMPLATES:
        src = path.read_text()
        comments = _HTML_COMMENT_RE.findall(src)
        for comment in comments:
            for token in _FORBIDDEN:
                assert token not in comment, (
                    f'forbidden substring {token!r} found in an HTML '
                    f'comment in {path}: {comment!r}'
                )
