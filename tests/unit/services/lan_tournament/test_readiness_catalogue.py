import functools
import pathlib
import re
import subprocess

from babel.messages.extract import extract_from_file
from babel.messages.pofile import read_po
import pytest


_ROOT = pathlib.Path(__file__).resolve().parents[4]
_PO_PATH = _ROOT / 'byceps/translations/de/LC_MESSAGES/messages.po'
_MODULE = _ROOT / 'byceps/services/lan_tournament'
_SITE = _MODULE / 'blueprints/site'
_ADMIN = _MODULE / 'blueprints/admin'
_SITE_TEMPLATES = _SITE / 'templates/site/lan_tournament'
_ADMIN_TEMPLATES = _ADMIN / 'templates/admin/lan_tournament'

# The `prd/fixes` commit the readiness repair is built on. It outlives an
# amend of the feature commit above it. No generated catalogue may differ
# from it.
_BASELINE = 'dee97679505b00d831d6dfe902d601697a5930e3'

_KEYWORDS = {
    '_': None,
    'gettext': None,
    'lazy_gettext': None,
    'ngettext': (1, 2),
}

_PLACEHOLDER = re.compile(r'%\((\w+)\)[#0 +-]*\d*(?:\.\d+)?[a-zA-Z]')
_ERR_CODE = re.compile(r"Err\(\s*'([a-z]+(?:_[a-z0-9]+)+)'")

_FEATURE_FILES = (
    _MODULE / 'blueprints/readiness_forms.py',
    _MODULE / 'lan_tournament_view_helpers.py',
    _ADMIN / 'views.py',
    _SITE / 'views.py',
    _ADMIN_TEMPLATES / '_readiness_bracket.html',
    _ADMIN_TEMPLATES / 'bracket.html',
    _ADMIN_TEMPLATES / 'matches_for_tournament.html',
    _ADMIN_TEMPLATES / 'view.html',
    _ADMIN_TEMPLATES / 'view_match.html',
    _SITE_TEMPLATES / '_match_readiness.html',
    _SITE_TEMPLATES / '_readiness_bracket.html',
    _SITE_TEMPLATES / '_standings.html',
    _SITE_TEMPLATES / 'bracket.html',
    _SITE_TEMPLATES / 'matches.html',
    _SITE_TEMPLATES / 'view.html',
    _SITE_TEMPLATES / 'view_match.html',
)

# Services return these codes in `Err`, and the views call `gettext` on them,
# so each code is a msgid of its own.
# fmt: off
_SERVICE_ERROR_CODES = (
    'csrf_invalid',
    'email_address_missing',
    'email_body_snippet_missing',
    'email_config_missing',
    'email_footer_missing',
    'email_subject_snippet_missing',
    'email_template_formatting_failed',
    'invalid_invitation_status',
    'invalid_match_side',
    'invalid_readiness_revision',
    'invitation_conflict',
    'invitation_dispatch_failed',
    'invitation_lease_expired',
    'invitation_not_found',
    'invitation_record_failed',
    'match_confirmed',
    'match_not_found',
    'match_requires_two_contestants',
    'match_tournament_mismatch',
    'pairing_time_before_start',
    'pairing_tournament_mismatch',
    'readiness_audit_failed',
    'readiness_conflict',
    'readiness_dispatch_failed',
    'readiness_forbidden',
    'readiness_format_unsupported',
    'readiness_pairing_invalid',
    'readiness_revision_exhausted',
    'readiness_subject_not_found',
    'recipient_not_on_unique_match_side',
    'tournament_not_found',
    'tournament_not_ongoing',
)

# English sentences returned in `Err` by the roster paths of the repair.
_SERVICE_ERROR_SENTENCES = (
    'Party does not belong to this tournament.',
    'Team does not belong to this tournament.',
)

# The eight gaps the planning run of `check_translations.py` reported. Six
# strings left the sources with the snake-case error codes. The two ready-since
# labels stayed on the admin match page.
_BASELINE_GAPS = (
    'Cannot change readiness of a confirmed match.',
    'Match sides are not fixed yet.',
    'No readiness to revoke.',
    'Readiness changed concurrently. Please reload and retry.',
    'Readiness for this side was not claimed.',
    'Side A ready since',
    'Side B ready since',
    'You are not assigned to either side of this match.',
)

# Msgids the extraction has to find. An empty or misdirected extraction would
# otherwise pass the catalogue test with nothing to check.
_CANARY_MSGIDS = (
    'DEFWIN',
    'I am not ready',
    'Not ready (on behalf of this side)',
    'Open (no readiness)',
    'Readiness claimed.',
    'Side A ready since',
)
# fmt: on


@functools.cache
def _catalog():
    with _PO_PATH.open('rb') as f:
        return read_po(f, locale='de')


def _german_forms(msgid):
    """Return the msgstr forms, or `None` if they do not count as German."""
    message = _catalog().get(msgid)
    if message is None or message.fuzzy:
        return None
    forms = message.string
    if not isinstance(forms, tuple):
        forms = (forms,)
    if not all(form and form.strip() for form in forms):
        return None
    return forms


def _placeholders(text):
    return set(_PLACEHOLDER.findall(text.replace('%%', '')))


def _same_placeholders(msgid, forms):
    expected = _placeholders(msgid)
    return all(_placeholders(form) == expected for form in forms)


def _override_templates():
    return sorted(
        _ROOT.glob('sites/*/template_overrides/site/lan_tournament/**/*.html')
    )


@functools.cache
def _source_texts():
    paths = (
        *_MODULE.rglob('*.py'),
        *_MODULE.rglob('*.html'),
        *_override_templates(),
    )
    return tuple(path.read_text(encoding='utf-8') for path in sorted(paths))


@functools.cache
def _feature_msgids():
    """Map each extracted msgid of the feature sources to its plural form."""
    found = {}
    paths = (*_FEATURE_FILES, *_override_templates())
    for path in paths:
        is_python = path.suffix == '.py'
        method = 'python' if is_python else 'jinja2.ext:babel_extract'
        options = {} if is_python else {'extensions': 'jinja2.ext.i18n'}
        for _, message, _, _ in extract_from_file(
            method, path, keywords=_KEYWORDS, options=options
        ):
            if isinstance(message, tuple):
                singular = message[0]
                plural = message[1] if len(message) > 1 else None
            else:
                singular, plural = message, None
            if singular:
                found[singular] = plural or found.get(singular)
    return found


def _git(*args):
    result = subprocess.run(  # noqa: S603 -- fixed arguments of this module
        ['git', *args],  # noqa: S607 -- git from PATH, read-only queries
        cwd=_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.splitlines()


def test_override_templates_are_part_of_the_extraction():
    names = {path.name for path in _override_templates()}

    assert {'index.html', 'my_requests.html', 'propose_form.html'} <= names


def test_extraction_finds_the_new_surfaces():
    found = _feature_msgids()

    assert sorted(set(_CANARY_MSGIDS) - found.keys()) == []


def test_feature_msgids_have_nonempty_german():
    missing = []
    wrong_placeholders = []
    for msgid, plural in sorted(_feature_msgids().items()):
        forms = _german_forms(msgid)
        if forms is None:
            missing.append(msgid)
        elif not _same_placeholders(plural or msgid, forms):
            wrong_placeholders.append(msgid)

    assert missing == []
    assert wrong_placeholders == []


@pytest.mark.parametrize(
    'msgid', _SERVICE_ERROR_CODES + _SERVICE_ERROR_SENTENCES
)
def test_service_errors_are_translated(msgid):
    forms = _german_forms(msgid)

    assert forms is not None, f'no German for {msgid!r}'
    assert forms != (msgid,), f'{msgid!r} is not translated'
    assert _same_placeholders(msgid, forms)


def test_every_snake_case_error_code_in_the_source_is_listed():
    found = set()
    for path in sorted(_MODULE.rglob('*.py')):
        found.update(_ERR_CODE.findall(path.read_text(encoding='utf-8')))

    assert sorted(found - set(_SERVICE_ERROR_CODES)) == []


@pytest.mark.parametrize('msgid', _BASELINE_GAPS)
def test_baseline_readiness_gap_is_resolved(msgid):
    still_used = any(msgid in source for source in _source_texts())

    assert _german_forms(msgid) is not None or not still_used


def test_no_generated_catalogue_changes():
    try:
        _git('cat-file', '-e', f'{_BASELINE}^{{commit}}')
    except subprocess.CalledProcessError:
        pytest.fail(
            f'Base commit {_BASELINE} is missing from this repository.'
            ' Fetch the history of the `prd/fixes` branch: without it the'
            ' check for generated catalogue changes cannot run.'
        )

    changed = _git(
        'diff',
        '--name-only',
        _BASELINE,
        '--',
        '*.mo',
        '*.pot',
        'byceps/translations',
    )
    untracked = _git(
        'ls-files',
        '--others',
        '--exclude-standard',
        '--',
        '*.mo',
        'byceps/translations',
    )

    assert [p for p in changed if not p.endswith('.po')] == []
    assert [p for p in untracked if not p.endswith('.po')] == []
