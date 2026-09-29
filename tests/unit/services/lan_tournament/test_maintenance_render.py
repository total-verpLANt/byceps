from datetime import datetime, UTC
import functools
import pathlib
from types import SimpleNamespace

from babel.messages.extract import extract_from_file
from babel.messages.pofile import read_po
from jinja2 import DictLoader, Environment, StrictUndefined
from markupsafe import Markup

from byceps.services.lan_tournament.tournament_maintenance_service import (
    MaintenanceItem,
    MaintenanceSummary,
)


_ROOT = pathlib.Path(__file__).resolve().parents[4]
_PO_PATH = _ROOT / 'byceps/translations/de/LC_MESSAGES/messages.po'
_TEMPLATE_DIR = (
    _ROOT
    / 'byceps/services/lan_tournament/blueprints/admin/templates'
    / 'admin/lan_tournament'
)
_TAB_TEMPLATE = _TEMPLATE_DIR / 'maintenance.html'
_PREVIEW_TEMPLATE = _TEMPLATE_DIR / 'maintenance_preview.html'

_LAYOUT = """
<head>{% block head %}<meta>{% endblock %}</head>
<main data-tab="{{ current_tab }}">{% block body %}{% endblock %}</main>
"""

_MACROS_ADMIN = """
{% macro render_extra_in_heading(value, label=None) -%}
<small>{{ value }}{% if label %} {{ label }}{% endif %}</small>
{%- endmacro %}
"""

_EXTRACTION_KEYWORDS = {
    '_': None,
    'gettext': None,
    'lazy_gettext': None,
    'ngettext': (1, 2),
}


def _make_env(templates):
    e = Environment(
        undefined=StrictUndefined,
        autoescape=True,
        loader=DictLoader(templates),
    )
    e.globals['_'] = lambda s, **kw: (s % kw) if kw else s
    e.globals['ngettext'] = lambda s, p, n, **kw: (
        (s if n == 1 else p) % {**kw, 'num': n}
    )
    e.globals['url_for'] = lambda endpoint, **kw: (
        endpoint + '?' + '&'.join(f'{k}={v}' for k, v in sorted(kw.items()))
    )
    return e


def _make_tab_env():
    return _make_env(
        {
            'maintenance.html': _TAB_TEMPLATE.read_text(),
            'layout/admin/lan_tournament.html': _LAYOUT,
            'macros/admin.html': _MACROS_ADMIN,
        }
    )


def _make_row(
    action_id, *, count, kept_count=0, finding='', kept_finding=None, hint=''
):
    return {
        'action': SimpleNamespace(id=action_id),
        'texts': {
            'title': f'Title {action_id}',
            'description': f'Description {action_id}',
            'empty': 'Nothing to clean up.',
            'empty_hint': hint,
        },
        'summary': MaintenanceSummary(
            count=count, byte_size=0, kept_count=kept_count
        ),
        'finding': finding,
        'kept_finding': kept_finding,
    }


def _render_tab(rows):
    party = SimpleNamespace(id='party-1', title='Party One')
    return (
        _make_tab_env()
        .get_template('maintenance.html')
        .render(party=party, rows=rows)
    )


def test_tab_template_renders_clean_and_work_rows():
    work = _make_row(
        'unused-images',
        count=12,
        kept_count=3,
        finding=Markup('<strong>12 images</strong>, together 18.4 MB.'),
        kept_finding='3 more are younger than 24 hours and stay.',
    )
    clean = _make_row(
        'orphaned-files',
        count=0,
        hint='An unused image appears here 24 hours after its upload.',
    )

    html = _render_tab([work, clean])

    assert 'data-tab="maintenance"' in html
    assert '<strong>12 images</strong>, together 18.4 MB.' in html
    assert '3 more are younger than 24 hours and stay.' in html
    assert html.count('Show preview') == 1
    assert (
        '.maintenance_preview?action_id=unused-images&amp;party_id=party-1'
        in html
    )
    assert 'action_id=orphaned-files' not in html
    assert 'Nothing to clean up.' in html
    assert 'An unused image appears here' in html
    assert html.count('is-clean') == 1
    assert 'lan_tournament_maintenance.css' in html


def test_tab_template_ignores_kept_sentence_on_a_clean_row():
    clean = _make_row(
        'unused-images',
        count=0,
        kept_count=2,
        kept_finding='2 more are younger than 24 hours and stay.',
    )

    html = _render_tab([clean])

    assert 'Nothing to clean up.' in html
    assert 'younger than 24 hours' not in html
    assert 'Show preview' not in html


@functools.cache
def _catalog():
    with _PO_PATH.open('rb') as f:
        return read_po(f, locale='de')


def _german_forms(msgid):
    message = _catalog().get(msgid)
    if message is None or message.fuzzy:
        return None
    forms = (
        message.string
        if isinstance(message.string, tuple)
        else (message.string,)
    )
    return forms if all(forms) else None


def _missing_german(msgids):
    return sorted(m for m in msgids if _german_forms(m) is None)


def _template_msgids():
    msgids = set()
    for path in sorted(_TEMPLATE_DIR.glob('maintenance*.html')):
        for _, message, _, _ in extract_from_file(
            'jinja2.ext:babel_extract',
            path,
            keywords=_EXTRACTION_KEYWORDS,
            options={'extensions': 'jinja2.ext.i18n'},
        ):
            msgid = message[0] if isinstance(message, tuple) else message
            if msgid:
                msgids.add(msgid)
    return msgids


def test_maintenance_templates_msgids_have_german():
    msgids = _template_msgids()

    assert 'Show preview' in msgids
    assert 'Uploaded by' in msgids
    assert _missing_german(msgids) == []


def test_missing_german_reports_an_unknown_msgid():
    assert _missing_german({'Show preview', 'No such msgid xyz'}) == [
        'No such msgid xyz'
    ]


def _make_item(key='a.png', *, orphan=False, **kwargs):
    fields = {
        'key': key,
        'file_name': key,
        'byte_size': 2048,
        'created_at': datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
        'creator_id': None if orphan else 'user-1',
        'width': None if orphan else 1600,
        'height': None if orphan else 900,
        'type_name': 'PNG',
        'thumbnail_url': None if orphan else f'/thumbs/{key}',
    }
    return MaintenanceItem(**{**fields, **kwargs})


def _make_preview_row(item, *, uploader='Alice'):
    return {
        'item': item,
        'size': '2 KB',
        'age': 'in 3 days',
        'created_at': '01.09.2026, 12:00',
        'uploader': uploader if item.creator_id else None,
    }


def _preview_texts(**overrides):
    return {
        'title': 'Unused tournament images',
        'empty': 'Nothing to clean up.',
        'empty_hint': 'An unused image appears here 24 hours after its upload.',
        'back': '‹ Maintenance',
        'select_all': 'Select all',
        'cancel': 'Cancel',
        'more_hint': 'After deleting, the next ones show up here.',
        'kept_heading': 'Stay',
        'kept_note': 'Younger than 24 hours.',
        'label_none': 'Nothing selected',
        'lead': 'Lead text.',
        'delete_hint': 'Deleted images cannot be restored.',
        'delete_button': 'Delete selected images',
        'label_one': 'Delete %(count)s image (%(size)s)',
        'label_many': 'Delete %(count)s images (%(size)s)',
        **overrides,
    }


def _render_preview(env=None, *, action_id='unused-images', **overrides):
    context = {
        'party': SimpleNamespace(id='party-1', title='Party One'),
        'action': SimpleNamespace(id=action_id),
        'texts': _preview_texts(),
        'rows': [],
        'kept_rows': [],
        'more_count': 0,
        'total_count': 0,
        'locale': 'en',
        **overrides,
    }
    env = env or _make_env(
        {
            'maintenance_preview.html': _PREVIEW_TEMPLATE.read_text(),
            'layout/admin/lan_tournament.html': _LAYOUT,
            'macros/admin.html': _MACROS_ADMIN,
        }
    )
    return env.get_template('maintenance_preview.html').render(**context)


def test_preview_template_renders_items_with_checked_boxes_and_labels():
    rows = [_make_preview_row(_make_item('a.png'))]
    kept = [_make_preview_row(_make_item('b.png'))]

    html = _render_preview(rows=rows, kept_rows=kept, total_count=1)

    assert html.count('name="key"') == 1
    assert 'name="key" value="a.png" checked data-bytes="2048"' in html
    assert 'data-maint-form' in html
    assert 'data-label-one="Delete %(count)s image (%(size)s)"' in html
    assert 'data-label-many="Delete %(count)s images (%(size)s)"' in html
    assert 'data-label-none="Nothing selected"' in html
    assert 'data-locale="en"' in html
    assert 'data-maint-all checked hidden' in html
    assert (
        '<button type="submit" class="button color-danger" data-maint-go>'
        in html
    )
    assert '1600 × 900, PNG' in html
    assert 'Alice' in html
    assert 'Stay' in html
    assert 'b.png' in html
    assert 'lan_tournament_maintenance.css' in html


def test_preview_template_renders_orphans_without_optional_fields():
    rows = [_make_preview_row(_make_item('c.png', orphan=True))]

    html = _render_preview(action_id='orphaned-files', rows=rows, total_count=1)

    assert 'value="c.png"' in html
    assert '<img' not in html
    assert 'Uploaded by' not in html
    assert 'Last modified' in html
    assert '01.09.2026, 12:00' in html
    assert '×' not in html.split('class="name"')[1].split('</span>')[0]


def test_preview_template_renders_empty_state():
    html = _render_preview()

    assert 'lt-maint-empty' in html
    assert 'Nothing to clean up.' in html
    assert 'An unused image appears here' in html
    assert '<form' not in html
    assert 'Stay' not in html
    assert 'Lead text.' not in html
    assert 'class="lead"' not in html


def test_preview_template_renders_no_lead_when_only_kept_rows_exist():
    kept = [_make_preview_row(_make_item('b.png'))]

    html = _render_preview(kept_rows=kept)

    assert 'lt-maint-empty' in html
    assert 'Stay' in html
    assert 'Lead text.' not in html


def test_preview_template_renders_lead_when_there_are_rows():
    rows = [_make_preview_row(_make_item('a.png'))]

    html = _render_preview(rows=rows, total_count=1)

    assert html.count('<p class="lead">Lead text.</p>') == 1


def test_preview_template_renders_compact_meta_lines():
    rows = [
        _make_preview_row(_make_item('a.png')),
        _make_preview_row(
            _make_item('nodims.png', width=None, height=None), uploader='Bob'
        ),
        _make_preview_row(_make_item('nobody.png'), uploader=None),
    ]

    html = _render_preview(rows=rows, total_count=3)

    assert html.count('class="lt-maint-mmeta"') == 6
    assert '<span class="lt-maint-mmeta">2 KB · 1600 × 900</span>' in html
    assert '<span class="lt-maint-mmeta">2 KB</span>' in html
    assert '<span class="lt-maint-mmeta">in 3 days, Alice</span>' in html
    assert '<span class="lt-maint-mmeta">in 3 days, Bob</span>' in html


def test_preview_template_renders_compact_meta_for_orphans_without_uploader():
    rows = [_make_preview_row(_make_item('c.png', orphan=True))]

    html = _render_preview(action_id='orphaned-files', rows=rows, total_count=1)

    assert '<span class="lt-maint-mmeta">2 KB</span>' in html
    assert '<span class="lt-maint-mmeta">in 3 days</span>' in html
    assert '×' not in html.split('lt-maint-mmeta')[1]


def test_preview_template_renders_more_hint_when_capped():
    rows = [_make_preview_row(_make_item('a.png'))]

    capped = _render_preview(rows=rows, more_count=5, total_count=6)
    complete = _render_preview(rows=rows, more_count=0, total_count=1)

    assert 'After deleting, the next ones show up here.' in capped
    assert '<small>6</small>' in capped
    assert 'After deleting, the next ones show up here.' not in complete


def test_preview_template_german_copy_matches_draft():
    def german(msgid):
        return _german_forms(msgid)[0]

    env = _make_env(
        {
            'maintenance_preview.html': _PREVIEW_TEMPLATE.read_text(),
            'layout/admin/lan_tournament.html': _LAYOUT,
            'macros/admin.html': _MACROS_ADMIN,
        }
    )
    env.globals['_'] = lambda s, **kw: german(s)
    texts = {
        key: german(msgid)
        for key, msgid in {
            'title': 'Unused tournament images',
            'empty': 'Nothing to clean up.',
            'empty_hint': 'An unused image appears here 24 hours after its'
            ' upload.',
            'back': '‹ Maintenance',
            'select_all': 'Select all',
            'cancel': 'Cancel',
            'more_hint': 'After deleting, the next ones show up here.',
            'kept_heading': 'Stay',
            'kept_note': 'Younger than 24 hours. One of them may be in an open'
            ' tournament wizard right now.',
            'label_none': 'Nothing selected',
            'lead': 'Deleted files cannot be restored.',
            'delete_hint': 'Deleted images cannot be restored.',
            'delete_button': 'Delete selected images',
            'label_one': 'Delete %(count)s image (%(size)s)',
            'label_many': 'Delete %(count)s images (%(size)s)',
        }.items()
    }

    empty = _render_preview(env, texts=texts)
    full = _render_preview(
        env,
        texts=texts,
        rows=[_make_preview_row(_make_item())],
        kept_rows=[_make_preview_row(_make_item('k.png'))],
    )

    assert 'Nichts aufzuräumen.' in empty
    assert '‹ Wartung' in empty
    assert 'Bleiben' in full
    assert 'Gelöschte Bilder lassen sich nicht wiederherstellen.' in full
    assert 'Alle auswählen' in full
    assert 'data-label-one="%(count)s Bild löschen (%(size)s)"' in full
    assert 'data-label-many="%(count)s Bilder löschen (%(size)s)"' in full
    assert 'data-label-none="Nichts ausgewählt"' in full
    assert 'Hochgeladen von' in full
