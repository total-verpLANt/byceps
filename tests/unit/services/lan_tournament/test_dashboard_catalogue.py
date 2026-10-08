import ast
from collections.abc import Callable, Iterable, Sequence
import copy
from dataclasses import dataclass, replace
import functools
import io
from pathlib import Path
import re
import subprocess

from babel.messages.catalog import Catalog, Message
from babel.messages.extract import extract
from babel.messages.pofile import read_po
import pytest


ROOT = Path(__file__).resolve().parents[4]
PO = ROOT / 'byceps/translations/de/LC_MESSAGES/messages.po'
MODULE = ROOT / 'byceps/services/lan_tournament'
ADMIN = MODULE / 'blueprints/admin'
SITE = MODULE / 'blueprints/site'
COMMON = MODULE / 'blueprints/common'
ADMIN_TEMPLATES = ADMIN / 'templates/admin/lan_tournament'
SITE_TEMPLATES = SITE / 'templates/site/lan_tournament'
OVERRIDES = 'sites/*/template_overrides/site/lan_tournament'
HELPERS = MODULE / 'dashboard_view_helpers.py'
SCRIPT = ROOT / 'byceps/static/behavior/lan_tournament_dashboard.js'

# The `prd/fixes` commit the F-03 branch is built on. It outlives an amend of
# the feature commit above it. No generated catalogue may differ from it.
BASELINE = 'dee97679505b00d831d6dfe902d601697a5930e3'

# Legacy services the dashboard integrates with. They are audited whole, so a
# msgid added there cannot go unlisted.
TOUCHED_SERVICES = (
    'tournament_match_service.py',
    'tournament_participant_service.py',
    'tournament_qualification_service.py',
    'tournament_repository.py',
    'tournament_seeding_service.py',
    'tournament_service.py',
    'tournament_team_service.py',
)

# Existing templates that carry a hunk of the dashboard: tab, entry button,
# return link and the Wartung card.
TOUCHED_TEMPLATES = (
    ADMIN_TEMPLATES / 'maintenance.html',
    ADMIN_TEMPLATES / 'view.html',
    ADMIN_TEMPLATES / 'view_match.html',
    ADMIN / 'templates/layout/admin/lan_tournament.html',
    SITE_TEMPLATES / 'index.html',
    SITE_TEMPLATES / 'view.html',
    SITE_TEMPLATES / 'view_match.html',
)

# The modules whose error codes the inventory below covers.
SERVICE_MODULES = tuple(
    f'byceps/services/lan_tournament/{name}'
    for name in (
        'blueprints/dashboard_csrf.py',
        'dashboard_config.py',
        'tournament_dashboard_coordination_service.py',
        'tournament_dashboard_service.py',
        'tournament_dashboard_settings_service.py',
        'tournament_operational_domain_service.py',
        'tournament_operational_service.py',
    )
)

# Services return these codes in `Err` (or as `*_ERROR` constants) and the
# views call `gettext` on them, so each code is a msgid of its own.
# `test_service_error_code_inventory_matches_the_sources` keeps this list and
# the sources equal in both directions.
# fmt: off
SERVICE_ERROR_CODES = (
    # Page, scope and query.
    'dashboard_unauthenticated',
    'dashboard_forbidden',
    'dashboard_query_invalid',
    # Pins and acknowledgements.
    'dashboard_match_not_found',
    'dashboard_match_terminal',
    'dashboard_pin_conflict',
    'dashboard_ack_conflict',
    'dashboard_ack_comment_invalid',
    'dashboard_ack_terminal',
    'dashboard_ack_paused',
    'dashboard_ack_not_due',
    'dashboard_ack_clock_unknown',
    'dashboard_ack_below_threshold',
    'dashboard_ack_recently_acknowledged',
    # The Wartung card and the deployment configuration.
    'dashboard_thresholds_stale',
    'invalid_dashboard_yellow_minutes',
    'invalid_dashboard_red_minutes',
    'invalid_dashboard_threshold_order',
    'invalid_dashboard_poll_seconds',
    'invalid_dashboard_page_size',
    # The operational clock.
    'lobby_not_free_for_all',
    'lobby_roster_incomplete',
    'unsupported_started_creation',
    'unsupported_status_transition',
    # The form check, and codes reused from the readiness repair.
    'csrf_invalid',
    'tournament_not_found',
    'match_not_found',
    'match_confirmed',
)
# fmt: on

SNAKE_CASE = re.compile(r'[a-z]+(?:_[a-z0-9]+)+')
PLACEHOLDER = re.compile(r'%\((\w+)\)[#0 +-]*\d*(?:\.\d+)?[a-zA-Z]')

TEXT_CALLS = frozenset({'gettext', 'lazy_gettext', '_'})
PLURAL_CALLS = frozenset({'ngettext', 'lazy_ngettext'})
CODE_CALLS = frozenset({'Err', 'ValidationMessage'})

JINJA_KEYWORDS = {
    '_': None,
    'gettext': None,
    'lazy_gettext': None,
    'ngettext': (1, 2),
}


# ------------------------------------------------------------- extraction


@dataclass(frozen=True)
class Source:
    path: str
    text: str


@dataclass(frozen=True)
class Found:
    msgid: str
    plural: str | None
    where: str
    kind: str  # `text`, `code` or `constant`


def read_source(path: Path) -> Source:
    text = path.read_text(encoding='utf-8')
    return Source(str(path.relative_to(ROOT)), text)


@functools.cache
def python_sources() -> tuple[Source, ...]:
    paths = {
        *MODULE.rglob('*dashboard*.py'),
        *MODULE.rglob('*operational*.py'),
        COMMON / 'views.py',
        ADMIN / 'views.py',
        SITE / 'views.py',
        *(MODULE / name for name in TOUCHED_SERVICES),
    }
    return tuple(read_source(path) for path in sorted(paths))


@functools.cache
def template_sources() -> tuple[Source, ...]:
    paths = {
        *MODULE.rglob('*dashboard*.html'),
        *COMMON.rglob('*.html'),
        *ROOT.glob(f'{OVERRIDES}/*dashboard*.html'),
        *ROOT.glob(f'{OVERRIDES}/index.html'),
        *TOUCHED_TEMPLATES,
    }
    return tuple(read_source(path) for path in sorted(paths))


def name_of(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def string_of(node: ast.AST, constants: dict[str, str]) -> str | None:
    """Return the string a node stands for, or `None` if it is dynamic."""
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = string_of(node.left, constants)
        right = string_of(node.right, constants)
        return None if left is None or right is None else left + right
    name = name_of(node)
    return constants.get(name) if name else None


def assignments(tree: ast.Module) -> Iterable[tuple[ast.expr, ast.expr, int]]:
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                yield target, node.value, node.lineno
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            yield node.target, node.value, node.lineno


def module_constants(trees: Iterable[ast.Module]) -> dict[str, str]:
    """Map the module-level string constants of all modules by name."""
    trees = list(trees)
    constants: dict[str, str] = {}
    for _ in range(2):  # a constant may name another one
        for tree in trees:
            for target, value, _ in assignments(tree):
                text = string_of(value, constants)
                if text is not None and isinstance(target, ast.Name):
                    constants[target.id] = text
    return constants


def extract_python(sources: Iterable[Source]) -> list[Found]:
    """Find the msgids of Python sources with the AST.

    Covers `gettext`-style calls, `Err(...)` codes and module-level
    `*_ERROR` constants, also those that `Err` or `gettext` receive by name.
    Arguments that are not static strings are skipped.
    """
    parsed = [(s, ast.parse(s.text, s.path)) for s in sources]
    constants = module_constants(tree for _, tree in parsed)

    found = []
    for source, tree in parsed:
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            name = name_of(node.func)
            msgid = string_of(node.args[0], constants)
            if not msgid:
                continue
            where = f'{source.path}:{node.lineno}'
            if name in TEXT_CALLS:
                found.append(Found(msgid, None, where, 'text'))
            elif name in PLURAL_CALLS and len(node.args) > 1:
                plural = string_of(node.args[1], constants)
                found.append(Found(msgid, plural, where, 'text'))
            elif name in CODE_CALLS:
                found.append(Found(msgid, None, where, 'code'))
        for target, value, lineno in assignments(tree):
            if isinstance(target, ast.Name) and target.id.endswith('_ERROR'):
                text = string_of(value, constants)
                if text:
                    where = f'{source.path}:{lineno}'
                    found.append(Found(text, None, where, 'constant'))
    return found


def extract_templates(sources: Iterable[Source]) -> list[Found]:
    """Find the msgids of Jinja templates with Babel's Jinja extractor."""
    found = []
    for source in sources:
        results = extract(
            'jinja2.ext:babel_extract',
            io.BytesIO(source.text.encode('utf-8')),
            keywords=JINJA_KEYWORDS,
            options={'extensions': 'jinja2.ext.i18n'},
        )
        for lineno, message, _, _ in results:
            if isinstance(message, tuple):
                msgid = message[0]
                plural = message[1] if len(message) > 1 else None
            else:
                msgid, plural = message, None
            if msgid:
                where = f'{source.path}:{lineno}'
                found.append(Found(msgid, plural, where, 'text'))
    return found


def extract_surface(
    python: Iterable[Source], templates: Iterable[Source]
) -> list[Found]:
    return extract_python(python) + extract_templates(templates)


@functools.cache
def surface() -> tuple[Found, ...]:
    return tuple(extract_surface(python_sources(), template_sources()))


# -------------------------------------------------------------- the audit

Lookup = Callable[[str], Message | None]


@functools.cache
def catalogue() -> Catalog:
    with PO.open('rb') as f:
        return read_po(f, locale='de')


def real_lookup(msgid: str) -> Message | None:
    return catalogue().get(msgid)


def placeholders(text: str) -> set[str]:
    return set(PLACEHOLDER.findall(text.replace('%%', '')))


def forms_of(message: Message) -> tuple[str, ...]:
    """Return the msgstr forms of a message, one for a singular."""
    string = message.string
    if isinstance(string, str):
        return (string,)
    return tuple(string or ())


def problem_of(item: Found, message: Message | None) -> str | None:
    """Return why a msgid does not count as translated, or `None`."""
    if message is None:
        return 'missing'
    if message.fuzzy:
        return 'fuzzy'
    forms = forms_of(message)
    if not forms or not all(form and form.strip() for form in forms):
        return 'empty'
    if item.plural:
        if len(forms) != 2:
            return 'plural_forms'
        pairs = list(zip(forms, (item.msgid, item.plural), strict=True))
    else:
        pairs = [(forms[0], item.msgid)]
    if any(placeholders(form) != placeholders(src) for form, src in pairs):
        return 'placeholders'
    return None


def audit(found: Iterable[Found], lookup: Lookup) -> dict[str, str]:
    """Map each msgid without proper German to the reason."""
    problems: dict[str, str] = {}
    for item in found:
        reason = problem_of(item, lookup(item.msgid))
        if reason:
            problems.setdefault(item.msgid, reason)
    return problems


def service_codes(found: Iterable[Found]) -> set[str]:
    """Return the snake-case codes of the dashboard's service modules."""
    return {
        item.msgid
        for item in found
        if item.kind in {'code', 'constant'}
        and item.where.startswith(SERVICE_MODULES)
        and SNAKE_CASE.fullmatch(item.msgid)
    }


# ------------------------------------------------------ labels and script


@functools.cache
def label_values() -> dict[str, ast.expr]:
    """Map each key of `dashboard_labels()` to its value expression."""
    tree = ast.parse(HELPERS.read_text(encoding='utf-8'))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == 'dashboard_labels'
    )
    result = next(
        node for node in ast.walk(function) if isinstance(node, ast.Return)
    )
    assert isinstance(result.value, ast.Dict)
    pairs = zip(result.value.keys, result.value.values, strict=True)
    return {
        key.value: value
        for key, value in pairs
        if isinstance(key, ast.Constant) and isinstance(key.value, str)
    }


def is_translated(value: ast.expr) -> bool:
    """Tell a `gettext` call, or the `str` of a translated notice."""
    if not isinstance(value, ast.Call):
        return False
    name = name_of(value.func)
    if name in TEXT_CALLS:
        return bool(value.args) and isinstance(value.args[0], ast.Constant)
    if name == 'str' and len(value.args) == 1:
        return (name_of(value.args[0]) or '').endswith('_NOTICE')
    return False


SCRIPT_KEY = re.compile(r"""\blabel\(\s*['"](\w+)['"]|\blabels\.(\w+)""")
TEMPLATE_KEY = re.compile(r"""\blabels\.(\w+)|\blabels\[\s*['"](\w+)['"]""")
DICT_METHODS = frozenset({'get', 'items', 'keys', 'values'})


def referenced_keys(pattern: re.Pattern[str], text: str) -> set[str]:
    keys = {part for groups in pattern.findall(text) for part in groups if part}
    return keys - DICT_METHODS


# ------------------------------------------------------ catalogue mutants


def mutated(lookup: Lookup, msgid: str, mutation: str) -> Lookup:
    """Return a lookup that differs from `lookup` in one message."""

    def mutant(requested: str) -> Message | None:
        message = lookup(requested)
        if requested != msgid or message is None:
            return message
        if mutation == 'removed':
            return None
        changed = copy.copy(message)
        changed.flags = set(message.flags)
        forms = forms_of(message)
        if mutation == 'fuzzy':
            changed.flags.add('fuzzy')
        elif mutation == 'emptied':
            changed.string = (*forms[:-1], '') if len(forms) > 1 else ''
        elif mutation == 'placeholder_dropped':
            first = PLACEHOLDER.sub('', forms[0], count=1)
            changed.string = (first, *forms[1:]) if len(forms) > 1 else first
        return changed

    return mutant


# fmt: off
REASONS = {
    'removed': 'missing',
    'emptied': 'empty',
    'fuzzy': 'fuzzy',
    'placeholder_dropped': 'placeholders',
}

# One msgid per way the dashboard reaches the catalogue.
MUTANT_MSGIDS = (
    'Check delay',                         # a label of the view helpers
    '%(count)d match',                     # a plural of the view helpers
    'Filtered to %(tier)s: %(count)s.',    # a label only the script reads
    'dashboard_ack_conflict',              # an error constant
    'lobby_roster_incomplete',             # an `Err` literal
    'Return target',                       # a form label
    'Dashboard',                           # the admin tab
    'Tournament office',                   # the totalverplant wrapper
    'Yellow from (minutes)',               # the Wartung card
    'Please log in.',                      # a route flash
)

MUTANTS = [
    (msgid, mutation)
    for msgid in MUTANT_MSGIDS
    for mutation in ('removed', 'emptied', 'fuzzy')
] + [
    ('Filtered to %(tier)s: %(count)s.', 'placeholder_dropped'),
    ('%(count)d match', 'placeholder_dropped'),
]
# fmt: on


# ---------------------------------------------------------- source mutants


def appended(
    sources: Sequence[Source], path_end: str, extra: str
) -> tuple[Source, ...]:
    """Return the sources with `extra` appended to one of them."""
    done = [
        replace(source, text=source.text + extra)
        if source.path.endswith(path_end)
        else source
        for source in sources
    ]
    assert done != list(sources), f'no source ends in {path_end!r}'
    return tuple(done)


# fmt: off
SOURCE_MUTANTS = [
    pytest.param(
        'python', 'dashboard_view_helpers.py',
        "\n\ndef _unlisted():\n    return gettext('An unlisted sentence.')\n",
        'An unlisted sentence.', id='gettext-call',
    ),
    pytest.param(
        'python', 'dashboard_view_helpers.py',
        "\n\ndef _unlisted(n):\n"
        "    return ngettext('%(n)d unlisted', '%(n)d unlisteds', n)\n",
        '%(n)d unlisted', id='ngettext-call',
    ),
    pytest.param(
        'python', 'tournament_dashboard_service.py',
        "\n\nNEW_UNLISTED_ERROR = 'new_unlisted_code'\n",
        'new_unlisted_code', id='error-constant',
    ),
    pytest.param(
        'python', 'tournament_operational_service.py',
        "\n\ndef _unlisted():\n    return Err('another_unlisted_code')\n",
        'another_unlisted_code', id='err-literal',
    ),
    pytest.param(
        'python', 'dashboard_forms.py',
        "\n\n_UNLISTED = lazy_gettext('An unlisted field label')\n",
        'An unlisted field label', id='lazy-gettext',
    ),
    pytest.param(
        'python', 'blueprints/site/views.py',
        "\n\ndef _unlisted():\n    return gettext('An unlisted flash.')\n",
        'An unlisted flash.', id='site-view',
    ),
    pytest.param(
        'template', '_dashboard_panel.html',
        "\n{{ _('An unlisted panel sentence.') }}\n",
        'An unlisted panel sentence.', id='common-template',
    ),
    pytest.param(
        'template', 'admin/lan_tournament/dashboard.html',
        '\n{% trans %}An unlisted trans block.{% endtrans %}\n',
        'An unlisted trans block.', id='trans-block',
    ),
    pytest.param(
        'template', 'template_overrides/site/lan_tournament/dashboard.html',
        "\n{{ _('An unlisted override sentence.') }}\n",
        'An unlisted override sentence.', id='totalverplant-override',
    ),
]
# fmt: on


# -------------------------------------------------------------- design copy

# The German of the design r1.1 copy table, by English msgid. The design
# document's string wins over a mockup variant, and D3 applies: „Prüfung
# erfassen…“ opens the form, „Prüfung festhalten“ submits it, „Nochmals
# prüfen“ opens a follow-up, and the yellow tier keeps „Verzögerung prüfen“.
# A plural is a pair of forms. Where the shipped German is not the design
# string, DESIGN_DEVIATIONS below says so.
# fmt: off
DESIGN_COPY = [
    # Title, entry, shells.
    ('Orga dashboard', 'Orga-Dashboard'),
    ('Dashboard', 'Dashboard'),
    ('Tournaments', 'Turniere'),
    ('Tournament office', 'Turnierbüro'),
    ('only for orgas', 'nur für Orgas'),
    ('What is waiting right now – across all your tournaments.',
     'Was gerade wartet – über alle deine Turniere.'),
    # Return link and the missing match.
    ('← Back to the orga dashboard', '← Zurück zum Orga-Dashboard'),
    ('Back to the orga dashboard', 'Zurück zum Orga-Dashboard'),
    ('This match is not available.', 'Diese Begegnung ist nicht verfügbar.'),
    ('It does not exist or you cannot see it.',
     'Sie existiert nicht oder du kannst sie nicht sehen.'),
    # Scope.
    ('Dashboard scope', 'Umfang'),
    ('Assigned tournaments', 'Zugewiesene Turniere'),
    ('All tournaments of this party', 'Alle Turniere dieser Party'),
    ('Apply scope', 'Umfang anwenden'),
    ('Fixed for site orgas', 'Fest für Site-Orgas'),
    # Freshness line and its states.
    ('As of', 'Stand'),
    ('(%(minutes)d min ago)', '(vor %(minutes)d min)'),
    ('Refreshes automatically every %(seconds)d s',
     'Automatisch alle %(seconds)d s'),
    ('Refreshing…', 'Wird aktualisiert…'),
    ('Automatic refresh paused while a form is being edited',
     ('Automatische Aktualisierung angehalten, solange ein Formular'
      ' bearbeitet wird')),
    ('Paused while the tab is in the background',
     'Angehalten, solange der Tab im Hintergrund ist'),
    ('Refresh failed – the data shown is out of date.',
     'Aktualisierung fehlgeschlagen – angezeigte Daten sind veraltet.'),
    ('Refresh stopped', 'Aktualisierung gestoppt'),
    ('Without JavaScript: manual refresh only',
     'Ohne JavaScript: nur manuelle Aktualisierung'),
    ('Refresh now', 'Jetzt aktualisieren'),
    ('Try again', 'Erneut versuchen'),
    ('Refreshed, as of %(time)s', 'Aktualisiert, Stand %(time)s'),
    # Tier tiles.
    ('Due matches by tier', 'Aktuell fällig nach Stufe'),
    ('· whole scope, independent of filters and page',
     '· gesamter Umfang, unabhängig von Filtern und Seite'),
    ('Long delay', 'Lange Verzögerung'),
    ('Check delay', 'Verzögerung prüfen'),
    ('Below warning threshold', 'Unter Warnschwelle'),
    ('from %(minutes)d min', 'ab %(minutes)d min'),
    ('%(low)d–%(high)d min', '%(low)d–%(high)d min'),
    ('under %(minutes)d min', 'unter %(minutes)d min'),
    ('%(count)d match: %(tier)s (%(range)s).',
     ('%(count)d Begegnung: %(tier)s (%(range)s).',
      '%(count)d Begegnungen: %(tier)s (%(range)s).')),
    ('Filter the list to this tier', 'Liste auf diese Stufe filtern'),
    ('Remove the filter, show all matches',
     'Filter aufheben, alle Begegnungen zeigen'),
    ('Filtered to %(tier)s: %(count)s.', 'Gefiltert auf %(tier)s: %(count)s.'),
    ('Filter removed. %(count)s.', 'Filter aufgehoben. %(count)s.'),
    ('Green does not mean ready to play. Readiness is shown separately.',
     'Grün bedeutet nicht spielbereit. Bereitschaft steht separat.'),
    # Views.
    ('Due now', 'Aktuell fällig'),
    ('Upcoming matches', 'Kommende Begegnungen'),
    ('All matches', 'Alle Begegnungen'),
    ('View', 'Ansicht'),
    ('Filter dashboard matches', 'Begegnungen filtern'),
    # Filters.
    ('Tournament', 'Turnier'),
    ('State', 'Zustand'),
    ('Sort order', 'Sortierung'),
    ('All assigned tournaments', 'Alle zugewiesenen Turniere'),
    ('All states', 'Alle Zustände'),
    ('Tier: %(tier)s', 'Stufe: %(tier)s'),
    ('Nobody ready', 'Niemand bereit'),
    ('One side ready', 'Eine Seite bereit'),
    ('Both ready', 'Beide bereit'),
    ('Readiness not available', 'Bereitschaft nicht verfügbar'),
    ('With conflict', 'Mit Konflikt'),
    ('Review open', 'Überprüfung offen'),
    ('Pinned', 'Angepinnt'),
    ('Urgency', 'Dringlichkeit'),
    ('Total active wait', 'Aktive Wartezeit gesamt'),
    ('Apply filters', 'Anwenden'),
    ('Reset', 'Zurücksetzen'),
    ('Applied filters', 'Angewendet'),
    ('Sort: %(sort)s', 'Sortierung: %(sort)s'),
    # The invalid query.
    ('This value is invalid and was not applied.',
     'Dieser Wert ist ungültig und wurde nicht angewendet.'),
    ('Part of the query was invalid and was not applied.',
     'Ein Teil der Abfrage war ungültig und wurde nicht angewendet.'),
    (('%(field)s “%(value)s” does not exist. The list is shown without this'
      ' value.'),
     ('%(field)s „%(value)s“ gibt es nicht. Angezeigt wird die Liste ohne'
      ' diesen Wert.')),
    ('This tournament is not available', 'Dieses Turnier ist nicht verfügbar'),
    # Count, context line and pager.
    ('%(count)d match', ('%(count)d Begegnung', '%(count)d Begegnungen')),
    ('Page %(page)d', 'Seite %(page)d'),
    ('Page %(page)d of %(pages)d', 'Seite %(page)d von %(pages)d'),
    ('Pages', 'Seiten'),
    ('‹ Previous', '‹ Vorherige'),
    ('Next ›', 'Nächste ›'),
    # Row context.
    ('Knockout', 'K.-o.'),
    ('Round robin', 'Jeder gegen jeden'),
    ('Free for all', 'Free-for-all'),
    ('Game unknown', 'Spiel unbekannt'),
    ('against', 'gegen'),
    ('Opponent still open', 'Gegner noch offen'),
    ('no opponent (bye)', 'kein Gegner (Freilos)'),
    ('Lobby %(n)s', 'Lobby %(n)s'),
    ('%(count)d participant',
     ('%(count)d teilnehmende Person', '%(count)d Teilnehmende')),
    ('Participants in %(lobby)s', 'Teilnehmende in %(lobby)s'),
    ('Responsible', 'Zuständig'),
    ('No orga assigned', 'Keine Orga hinterlegt'),
    # Phase, bracket and match labels. The design's fixtures read „Winner
    # Bracket“ and „Loser Bracket“; the words of the module ship instead.
    ('Playoffs', 'Playoffs'),
    ('Winners bracket', 'Gewinnerrunde'),
    ('Losers bracket', 'Verliererrunde'),
    ('Winners pool', 'Gewinner-Pool'),
    ('Losers pool', 'Verlierer-Pool'),
    ('Grand final', 'Großes Finale'),
    ('Third place', 'Spiel um Platz 3'),
    ('Group %(letter)s', 'Gruppe %(letter)s'),
    ('Round %(num)s', 'Runde %(num)s'),
    ('Game %(num)d', 'Spiel %(num)d'),
    # Readiness.
    ('Readiness', 'Bereitschaft'),
    ('%(side)s ready since %(time)s', '%(side)s bereit seit %(time)s'),
    ('%(side)s not ready', '%(side)s nicht bereit'),
    ('Not available (lobby format)', 'Nicht verfügbar (Lobby-Format)'),
    ('Not available', 'Nicht verfügbar'),
    # The tier cell.
    ('Alert interval', 'Alarmintervall'),
    ('Alert interval, frozen', 'Alarmintervall, eingefroren'),
    ('Paused', 'Pausiert'),
    ('Wait time frozen', 'Wartezeit eingefroren'),
    ('Upcoming', 'Kommend'),
    ('Not due yet · no tier', 'Noch nicht fällig · keine Stufe'),
    ('Incomplete', 'Unvollständig'),
    ('Opponent open · not playable', 'Gegner offen · nicht spielbar'),
    ('Bye', 'Freilos'),
    ('No match needed', 'Kein Spiel nötig'),
    ('Waiting for lobby', 'Wartet auf Lobby'),
    ('Lobby not complete yet', 'Lobby noch nicht vollständig'),
    ('Completed', 'Abgeschlossen'),
    ('Confirmed · no actions', 'Bestätigt · keine Aktionen'),
    ('Time unknown', 'Zeit unbekannt'),
    ('No tier without a time base', 'Keine Stufe ohne Zeitbasis'),
    # Times and durations.
    ('Occupied since', 'Besetzt seit'),
    ('Due since (this episode)', 'Fällig seit (diese Episode)'),
    ('Last match change', 'Letzte Match-Änderung'),
    ('Created', 'Erstellt'),
    ('%(time)s · age %(age)s', '%(time)s · Alter %(age)s'),
    ('%(duration)s, frozen', '%(duration)s, eingefroren'),
    ('%(duration)s (until confirmation)', '%(duration)s (bis Bestätigung)'),
    ('Not due yet', 'Noch nicht fällig'),
    ('Not yet occupied', 'Noch nicht besetzt'),
    ('Historical time unknown', 'Historischer Zeitpunkt unbekannt'),
    ('under 1 min', 'unter 1 min'),
    ('%(minutes)d min', '%(minutes)d min'),
    ('%(hours)d h %(minutes)02d min', '%(hours)d h %(minutes)02d min'),
    # Signals.
    ('Pinned by %(actor)s at %(time)s', 'Angepinnt von %(actor)s um %(time)s'),
    ('Conflict', 'Konflikt'),
    ('Review: not available', 'Überprüfung: nicht verfügbar'),
    ('New episode since %(time)s', 'Neue Episode seit %(time)s'),
    ('Deleted orga', 'Gelöschte Orga'),
    # Conflicts.
    ('(member of %(team)s)', '(Mitglied von %(team)s)'),
    ('is needed in another match at the same time:',
     'ist gleichzeitig in einer anderen Begegnung benötigt:'),
    ('as member of %(team)s', 'als Mitglied von %(team)s'),
    ('To the match', 'Zur Begegnung'),
    ('In this list: page %(page)d', 'In dieser Liste: Seite %(page)d'),
    ('is needed in a match outside your tournaments at the same time.',
     ('wird gleichzeitig in einer Begegnung außerhalb deiner Turniere'
      ' benötigt.')),
    # The acknowledgement: D3, then the reasons it is not offered.
    ('Log a check…', 'Prüfung erfassen…'),
    ('Check again', 'Nochmals prüfen'),
    ('Record the check', 'Prüfung festhalten'),
    ('Cancel', 'Abbrechen'),
    ('Delay checked – record it', 'Verzögerung geprüft – festhalten'),
    ('Delay checked again – record it',
     'Erneute Verzögerung geprüft – festhalten'),
    ('Checks start at %(minutes)d min of alert interval.',
     'Prüfen ab %(minutes)d min Alarmintervall.'),
    ('Just checked. Again from %(minutes)d min of alert interval.',
     'Gerade geprüft. Erneut ab %(minutes)d min Alarmintervall.'),
    ('Paused – checking not possible.', 'Pausiert – Prüfen nicht möglich.'),
    ('Not due – no checking.', 'Nicht fällig – kein Prüfen.'),
    ('No time base, no checking.', 'Ohne Zeitbasis kein Prüfen.'),
    ('Completed – no actions.', 'Abgeschlossen – keine Aktionen.'),
    # The acknowledgement form.
    (('Records that you have checked the delay. Only resets the alert'
      ' interval, not the total wait. Visible to all orgas of this'
      ' tournament, not to players.'),
     ('Hält fest, dass du die Verzögerung geprüft hast. Setzt nur das'
      ' Alarmintervall zurück, nicht die gesamte Wartezeit. Sichtbar für'
      ' alle Orgas dieses Turniers, nicht für Spielende.')),
    ('Comment (optional)', 'Kommentar (optional)'),
    ('%(count)d / %(max)d characters · text only',
     '%(count)d / %(max)d Zeichen · nur Text'),
    (('Comment is too long: %(count)d of at most 500 characters. Nothing was'
      ' saved.'),
     ('Kommentar ist zu lang: %(count)d von höchstens 500 Zeichen. Nichts'
      ' wurde gespeichert.')),
    ('Saving…', 'Wird gespeichert…'),
    ('Not confirmed yet.', 'Noch nicht bestätigt.'),
    # The outcomes of an acknowledgement.
    (('Check recorded (server time %(time)s). The alert interval restarts;'
      ' total wait unchanged at %(wait)s.'),
     ('Prüfung festgehalten (Server-Stand %(time)s). Alarmintervall läuft'
      ' neu; Wartezeit gesamt unverändert %(wait)s.')),
    ('dashboard_ack_conflict',
     'Der Stand hat sich geändert. Bitte aktualisieren und erneut prüfen.'),
    ('dashboard_ack_paused', 'Das Turnier ist pausiert.'),
    (('%(actor)s already recorded this delay at %(time)s. Your draft is kept'
      ' below and was not sent.'),
     ('%(actor)s hat diese Verzögerung um %(time)s bereits festgehalten. Dein'
      ' Entwurf ist unten erhalten und wurde nicht gesendet.')),
    ('Refresh now · keep draft', 'Jetzt aktualisieren · Entwurf behalten'),
    ('Check not saved – connection failed. Try again.',
     ('Prüfung nicht gespeichert – Verbindung fehlgeschlagen. Erneut'
      ' versuchen.')),
    ('Not recorded: %(reason)s', 'Nicht festgehalten: %(reason)s'),
    (('The row now shows the server state. Your draft stays available for'
      ' copying.'),
     ('Die Zeile zeigt jetzt den Server-Stand. Dein Entwurf bleibt zum'
      ' Kopieren erhalten.')),
    ('Check recorded: %(match)s.', 'Prüfung festgehalten: %(match)s.'),
    ('As of %(time)s. The alert interval restarts.',
     'Stand %(time)s. Alarmintervall läuft neu.'),
    ('Check recorded.', 'Prüfung festgehalten.'),
    # The record and its history.
    ('Last checked by %(actor)s at %(time)s',
     'Zuletzt geprüft von %(actor)s um %(time)s'),
    ('Delay again – check once more', 'Erneute Verzögerung – nochmals prüfen'),
    ('Show recent checks', 'Letzte Prüfungen anzeigen'),
    ('Hide recent checks', 'Letzte Prüfungen ausblenden'),
    ('(%(count)d in this episode)', '(%(count)d in dieser Episode)'),
    ('current', 'aktuell'),
    ('no comment', 'ohne Kommentar'),
    # The pin.
    ('Pin for the orga team', 'Für das Orga-Team anpinnen'),
    ('Remove pin', 'Pin entfernen'),
    ('Pinning…', 'Wird angepinnt…'),
    ('Removing…', 'Wird entfernt…'),
    ('Pinned (server time %(server)s)', 'Angepinnt (Server-Stand %(server)s)'),
    ('Pin removed.', 'Pin entfernt.'),
    ('The state has changed. Please refresh and try again.',
     'Der Stand hat sich geändert. Bitte aktualisieren und erneut versuchen.'),
    ('Pin not saved – connection failed. Try again.',
     'Pin nicht gespeichert – Verbindung fehlgeschlagen. Erneut versuchen.'),
    # Links.
    ('To the tournament', 'Zum Turnier'),
    # Empty states: no assignment, no demand, no actionable fixtures, no
    # filter match, a page that emptied.
    ('You have no tournaments assigned at this party.',
     'Dir sind auf dieser Party keine Turniere zugewiesen.'),
    ('As an administrator you can view all tournaments of this party.',
     'Als Administrator kannst du alle Turniere dieser Party ansehen.'),
    ('Show all tournaments of this party',
     'Alle Turniere dieser Party anzeigen'),
    (('To be assigned a tournament, the tournament team has to enter you as'
      ' orga.'),
     ('Wenn du ein Turnier betreuen sollst, muss dich die Turnierleitung als'
      ' Orga eintragen.')),
    ('To the tournament overview', 'Zur Turnierübersicht'),
    ('No match is due right now.', 'Gerade ist keine Begegnung fällig.'),
    ('No playable match is waiting in your tournaments at the moment.',
     'In deinen Turnieren wartet derzeit keine spielbare Begegnung.'),
    ('Show upcoming matches', 'Kommende Begegnungen anzeigen'),
    ('No match is due at the moment.', 'Keine Begegnung ist aktuell fällig.'),
    (('%(count)d match in your tournaments has no current demand:'
      ' %(paused)d paused, %(pre_start)d before the tournament start,'
      ' %(partial)d incomplete.'),
     (('%(count)d Begegnung in deinen Turnieren hat keinen aktuellen Bedarf:'
       ' %(paused)d pausiert, %(pre_start)d vor dem Turnierstart,'
       ' %(partial)d unvollständig.'),
      ('%(count)d Begegnungen in deinen Turnieren haben keinen aktuellen'
       ' Bedarf: %(paused)d pausiert, %(pre_start)d vor dem Turnierstart,'
       ' %(partial)d unvollständig.'))),
    ('Show all matches', 'Alle Begegnungen anzeigen'),
    ('No match fits these filters.', 'Keine Begegnung passt zu diesen Filtern.'),
    ('Reset filters', 'Filter zurücksetzen'),
    ('Page %(page)d is empty now.', 'Seite %(page)d ist jetzt leer.'),
    ('Matches have been confirmed since your last refresh.',
     'Seit deinem letzten Stand sind Begegnungen bestätigt worden.'),
    ('There is only %(count)d page left.',
     ('Es gibt nur noch %(count)d Seite.', 'Es gibt nur noch %(count)d Seiten.')),
    ('To page 1', 'Zu Seite 1'),
    ('Page %(page)d no longer exists', 'Seite %(page)d gibt es nicht mehr'),
    # The leaderboard card.
    ('%(tournament)s · %(game)s', '%(tournament)s · %(game)s'),
    (('Highscore tournament with a plain leaderboard – there are no matches'
      ' and no readiness. Playoffs would appear in their actual format.'),
     ('Highscore-Turnier mit reiner Rangliste – es gibt keine Begegnungen und'
      ' keine Bereitschaft. Playoffs würden im tatsächlichen Format'
      ' erscheinen.')),
    ('Open leaderboard', 'Rangliste öffnen'),
    # Row notes.
    (('Round %(round)d of %(group)s is still open. Future match, so no'
      ' conflict.'),
     ('Runde %(round)d der %(group)s ist noch offen. Künftige Begegnung,'
      ' daher kein Konflikt.')),
    ('%(name)s advances without a match.', '%(name)s rückt ohne Spiel weiter.'),
    ('%(filled)d of %(size)d places filled.',
     '%(filled)d von %(size)d Plätzen besetzt.'),
    ('The lobby is only created completely after round %(round)d.',
     'Die Lobby wird erst nach Runde %(round)d vollständig erzeugt.'),
    ('Result %(score)s confirmed at %(time)s.',
     'Ergebnis %(score)s bestätigt um %(time)s.'),
    (('The result was corrected and reopened at %(time)s. Earlier checks'
      ' belong to the previous episode and do not apply here.'),
     ('Ergebnis wurde um %(time)s korrigiert und wieder geöffnet. Frühere'
      ' Prüfungen gehören zur vorherigen Episode und gelten hier nicht.')),
    # Failure, session and access loss, and the draft card.
    (('As of %(time)s. Tiers and times may have changed since. Actions are'
      ' checked by the server.'),
     ('Stand %(time)s. Stufen und Zeiten können inzwischen anders sein.'
      ' Aktionen werden vom Server geprüft.')),
    ('Your session has expired. Refreshing was stopped.',
     'Deine Sitzung ist abgelaufen. Die Aktualisierung wurde gestoppt.'),
    ('The list was hidden. After signing in you return to the same view.',
     ('Die Liste wurde ausgeblendet. Nach dem Anmelden kommst du zu derselben'
      ' Ansicht zurück.')),
    ('Sign in', 'Anmelden'),
    ('No access any more. Refreshing was stopped.',
     'Kein Zugriff mehr. Die Aktualisierung wurde gestoppt.'),
    (('You can no longer see the orga dashboard. Contact the tournament team'
      ' with any questions.'),
     ('Du kannst das Orga-Dashboard nicht mehr sehen. Wende dich bei Fragen'
      ' an die Turnierleitung.')),
    ('Unsent draft', 'Nicht gesendeter Entwurf'),
    (('Your comment was not sent. Copy it if you need it; it is discarded'
      ' when you leave the page.'),
     ('Dein Kommentar wurde nicht gesendet. Kopiere ihn bei Bedarf; er wird'
      ' beim Verlassen der Seite verworfen.')),
    ('Draft (read only)', 'Entwurf (nur lesen)'),
    ('Keep the draft and refresh the list?',
     'Entwurf behalten und Liste aktualisieren?'),
    ('%(match)s is no longer due (confirmed) and was removed.',
     '%(match)s ist nicht mehr fällig (bestätigt) und wurde entfernt.'),
    ('Focus is on the next match.', 'Der Fokus liegt auf der nächsten Begegnung.'),
    # R44: the expired form check.
    (('The form check has expired. Please reload the page; your draft stays'
      ' visible.'),
     ('Die Formularprüfung ist abgelaufen. Bitte die Seite neu laden; dein'
      ' Entwurf bleibt sichtbar.')),
    # R43: the Wartung card.
    ('Orga dashboard: warning thresholds', 'Orga-Dashboard: Warnschwellen'),
    ('Applies to all tournaments of this party.',
     'Gilt für alle Turniere dieser Party.'),
    ('Yellow from (minutes)', 'Gelb ab (Minuten)'),
    ('Red from (minutes)', 'Rot ab (Minuten)'),
    ('Installation default: %(yellow)d/%(red)d min',
     'Standard der Installation: %(yellow)d/%(red)d min'),
    ('Set for this party by %(actor)s on %(date)s',
     'Für diese Party gesetzt von %(actor)s am %(date)s'),
    ('Save', 'Speichern'),
    ('Reset to default', 'Auf Standard zurücksetzen'),
    ('Yellow must be at least 1 minute.', 'Gelb muss mindestens 1 Minute sein.'),
    ('Red must be greater than yellow.', 'Rot muss größer als Gelb sein.'),
    ('At most 1440 minutes.', 'Höchstens 1440 Minuten.'),
    ('The thresholds were changed in the meantime. Please reload.',
     'Die Schwellen wurden inzwischen geändert. Bitte neu laden.'),
]

# Where the shipped German is not the design string, by msgid: the design
# string, and why it is not shipped. Each of these is a recorded decision, not
# an accident. The row in DESIGN_COPY pins what ships; this table makes the
# difference explicit and fails once the two agree.
DESIGN_DEVIATIONS = {
    # `%(count)s` is the text of the count heading; a template without a
    # placeholder would break the `_template` label rule of the view helpers.
    'Filter removed. %(count)s.': 'Filter aufgehoben. Alle Begegnungen.',
    # Examples the design gives for rows and reasons: the status note has no
    # person in its data, and the refusal code carries no time.
    ('Round %(round)d of %(group)s is still open. Future match, so no'
     ' conflict.'):
        ('Runde %(round)d der %(group)s ist noch offen. Kein Konflikt für'
         ' %(name)s: künftige Begegnung.'),
    'dashboard_ack_paused': 'Das Turnier wurde um %(time)s pausiert.',
    # The design's fixtures read Winner Bracket and Loser Bracket; the module
    # ships Gewinnerrunde and Verliererrunde (user decision).
    'Winners bracket': 'Winner Bracket',
    'Losers bracket': 'Loser Bracket',
}

# The msgids of the view helpers that are not design copy: variants of a
# design string for data the design does not draw, and bare nouns.
OWN_WORDING = frozenset({
    'Page',  # the noun of a dropped query value
    'Open',  # a side that has not claimed readiness
    '%(filled)d place filled.',  # a lobby note without its size
    'Result %(score)s confirmed.',  # a result note without its time
    'Round %(round)d is still open. Future match, so no conflict.',
    ('The result was corrected and reopened. Earlier checks belong to the'
     ' previous episode and do not apply here.'),
    ('Tournament “%(value)s” is not available. The list is shown without'
     ' this value.'),
})
# fmt: on

JINJA_MARKUP = re.compile(r'\{\{.*?\}\}|\{%.*?%\}|\{#.*?#\}', re.DOTALL)
HTML_TAG = re.compile(r'<[^>]*>')
LETTER = re.compile(r'[^\W\d_]')


# --------------------------------------------------------------- the tests


def test_extraction_reaches_every_dashboard_surface():
    found = surface()
    paths = {item.where.rsplit(':', 1)[0] for item in found}
    msgids = {item.msgid for item in found}

    # Each kind of source contributes, so a broken glob or a parser that
    # silently reads nothing cannot make the audit vacuous.
    for suffix in (
        'lan_tournament/dashboard_view_helpers.py',
        'blueprints/dashboard_forms.py',
        'blueprints/dashboard_csrf.py',
        'lan_tournament/dashboard_config.py',
        'tournament_dashboard_coordination_service.py',
        'tournament_dashboard_service.py',
        'tournament_dashboard_settings_service.py',
        'tournament_operational_service.py',
        'blueprints/admin/views.py',
        'blueprints/site/views.py',
        'admin/lan_tournament/dashboard.html',
        'site/lan_tournament/dashboard.html',
        'admin/lan_tournament/maintenance.html',
        'layout/admin/lan_tournament.html',
        'site/lan_tournament/index.html',
        'template_overrides/site/lan_tournament/dashboard.html',
        'template_overrides/site/lan_tournament/index.html',
    ):
        assert any(path.endswith(suffix) for path in paths), suffix

    # The common panel and row templates take their text from the labels,
    # so they carry no msgid of their own. They are still audited sources.
    audited = {source.path for source in template_sources()}
    for name in (
        '_dashboard_panel.html',
        '_dashboard_rows.html',
        '_bote_dashboard_style.html',
    ):
        assert any(path.endswith(name) for path in audited), name

    # A msgid of every kind is in the extraction.
    assert {
        'Orga dashboard',  # helpers and every wrapper
        'Dashboard',  # the admin tab
        'only for orgas',  # the site and totalverplant wrappers
        'Tournament office',  # the totalverplant wrapper
        # The totalverplant sub line.
        'What is waiting right now – across all your tournaments.',
        'Yellow from (minutes)',  # the Wartung card
        'Return target',  # the pin and ack forms
        'Please log in.',  # a route flash
        'Filtered to %(tier)s: %(count)s.',  # a label of the script
        'dashboard_ack_conflict',  # an error constant
        'lobby_roster_incomplete',  # an error constant, used in `Err`
        'tournament_not_found',  # an `Err` literal
        'csrf_invalid',  # a code that is also a notice
        (
            'The form check has expired. Please reload the page;'
            ' your draft stays visible.'
        ),
    } <= msgids
    plurals = {item.msgid: item.plural for item in found if item.plural}
    assert plurals['%(count)d match'] == '%(count)d matches'
    assert len(msgids) > 300


def test_dashboard_msgids_have_german():
    assert audit(surface(), real_lookup) == {}


@pytest.mark.parametrize('code', SERVICE_ERROR_CODES)
def test_service_error_codes_are_translated(code):
    message = real_lookup(code)

    assert problem_of(Found(code, None, '', 'code'), message) is None
    assert message.string != code, f'{code!r} is not translated'


def test_service_error_code_inventory_matches_the_sources():
    found = service_codes(surface())

    assert sorted(found - set(SERVICE_ERROR_CODES)) == []
    assert sorted(set(SERVICE_ERROR_CODES) - found) == []


def test_every_dashboard_label_is_translated_text():
    labels = label_values()

    assert len(labels) > 150
    untranslated = [k for k, v in labels.items() if not is_translated(v)]
    assert untranslated == []


def test_the_script_and_the_templates_read_only_labels_the_server_sends():
    sent = set(label_values())

    script = referenced_keys(SCRIPT_KEY, SCRIPT.read_text(encoding='utf-8'))
    assert script
    assert sorted(script - sent) == []
    # The three announcements and the failure text the script needs.
    assert {'tile_filtered_template', 'tile_unfiltered_template'} <= script
    assert 'ack_failed' in script

    for source in template_sources():
        if 'dashboard' in source.path:
            keys = referenced_keys(TEMPLATE_KEY, source.text)
            assert sorted(keys - sent) == [], source.path


@pytest.mark.parametrize('msgid, mutation', MUTANTS)
def test_catalogue_checker_detects_missing_copy(msgid, mutation):
    found = surface()
    assert audit(found, real_lookup) == {}

    problems = audit(found, mutated(real_lookup, msgid, mutation))

    assert problems == {msgid: REASONS[mutation]}


@pytest.mark.parametrize('kind, path_end, extra, msgid', SOURCE_MUTANTS)
def test_catalogue_checker_reads_the_sources_it_is_given(
    kind, path_end, extra, msgid
):
    python, templates = python_sources(), template_sources()
    if kind == 'python':
        python = appended(python, path_end, extra)
    else:
        templates = appended(templates, path_end, extra)

    found = extract_surface(python, templates)

    assert audit(found, real_lookup) == {msgid: 'missing'}


def test_a_new_error_code_must_be_listed_in_the_inventory():
    python = appended(
        python_sources(),
        'tournament_dashboard_service.py',
        "\n\nNEW_UNLISTED_ERROR = 'new_unlisted_code'\n",
    )

    found = service_codes(extract_python(python))

    assert found - set(SERVICE_ERROR_CODES) == {'new_unlisted_code'}


def test_no_generated_catalogue_assets_changed():
    try:
        git('cat-file', '-e', f'{BASELINE}^{{commit}}')
    except subprocess.CalledProcessError:
        pytest.fail(
            f'Base commit {BASELINE} is missing from this repository.'
            ' Fetch the history of the `prd/fixes` branch: without it the'
            ' check for generated catalogue changes cannot run.'
        )

    changed = git(
        'diff',
        '--name-only',
        BASELINE,
        '--',
        '*.mo',
        '*.pot',
        'byceps/translations',
    )
    untracked = git(
        'ls-files',
        '--others',
        '--exclude-standard',
        '--',
        '*.mo',
        'byceps/translations',
    )

    # The `.po` is the only catalogue file that may differ.
    assert generated_assets(changed) == []
    assert generated_assets(untracked) == []

    # The sources and tests of the dashboard compile the `.po` in memory only.
    assert catalogue_writers() == []


def generated_assets(paths: Iterable[str]) -> list[str]:
    """Return the paths that are not a `.po` source catalogue."""
    return [path for path in paths if not path.endswith('.po')]


def git(*args: str) -> list[str]:
    result = subprocess.run(  # noqa: S603 -- fixed arguments of this module
        ['git', *args],  # noqa: S607 -- git from PATH, read-only queries
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.splitlines()


def writer_lines(source: str) -> list[int]:
    """Find the lines that could write a compiled catalogue to disk.

    A `write_mo` into a `BytesIO` stays in memory and does not count.
    """
    tree = ast.parse(source)
    buffers = {
        target.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Call)
        and name_of(node.value.func) == 'BytesIO'
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    lines = []
    for call in ast.walk(tree):
        if not isinstance(call, ast.Call):
            continue
        name = name_of(call.func)
        first = call.args[0] if call.args else None
        words = {
            part.value
            for arg in call.args
            for part in ast.walk(arg)
            if isinstance(part, ast.Constant) and isinstance(part.value, str)
        }
        in_memory = isinstance(first, ast.Name) and first.id in buffers
        if (
            name == 'compile_catalog'
            or (name == 'write_mo' and not in_memory)
            or words & {'pybabel', 'msgfmt'}
        ):
            lines.append(call.lineno)
    return lines


def catalogue_writers() -> list[str]:
    """Find sources and tests of the dashboard that could write a catalogue."""
    paths = {
        *(ROOT / source.path for source in python_sources()),
        *ROOT.glob('tests/**/lan_tournament/*dashboard*.py'),
        *ROOT.glob('tests/**/lan_tournament/*operational*.py'),
    } - {Path(__file__).resolve()}

    return [
        f'{path.relative_to(ROOT)}:{line}'
        for path in sorted(paths)
        for line in writer_lines(path.read_text(encoding='utf-8'))
    ]


# fmt: off
@pytest.mark.parametrize('source, expected', [
    ('write_mo(buffer, catalog)\n', [1]),
    ('buffer = io.BytesIO()\nwrite_mo(buffer, catalog)\n', []),
    ('buffer = BytesIO()\nwrite_mo(open("x.mo", "wb"), catalog)\n', [2]),
    ('write_mo(path.open("wb"), catalog)\n', [1]),
    ('compile_catalog(directory=d)\n', [1]),
    ('subprocess.run(["pybabel", "compile"])\n', [1]),
    ('subprocess.run(["msgfmt", "-o", "x.mo", "x.po"])\n', [1]),
    ('catalog = read_po(f, locale="de")\n', []),
])
# fmt: on
def test_catalogue_writers_are_recognized(source, expected):
    assert writer_lines(source) == expected


def test_only_po_files_count_as_source_catalogues():
    paths = [
        'byceps/translations/de/LC_MESSAGES/messages.po',
        'byceps/translations/de/LC_MESSAGES/messages.mo',
        'byceps/translations/messages.pot',
    ]

    assert generated_assets(paths) == paths[1:]


@pytest.mark.parametrize('msgid, german', DESIGN_COPY)
def test_design_copy_is_the_german_translation(msgid, german):
    message = real_lookup(msgid)

    assert message is not None, f'{msgid!r} is not in the catalogue'
    assert not message.fuzzy
    assert message.string == german


def test_design_copy_table_has_no_duplicate_msgids():
    msgids = [msgid for msgid, _ in DESIGN_COPY]

    assert len(msgids) == len(set(msgids))


def test_design_deviations_are_recorded_and_still_deviate():
    pinned = dict(DESIGN_COPY)

    for msgid, design in DESIGN_DEVIATIONS.items():
        assert msgid in pinned, msgid
        assert pinned[msgid] != design, f'{msgid!r} now matches the design'


def test_every_msgid_of_the_view_helpers_is_design_copy_or_declared():
    pinned = {msgid for msgid, _ in DESIGN_COPY}
    helpers = str(HELPERS.relative_to(ROOT))
    used = {item.msgid for item in surface() if item.where.startswith(helpers)}

    assert sorted(used - pinned - OWN_WORDING) == []
    assert sorted(OWN_WORDING - used) == [], 'stale declaration'
    assert sorted(OWN_WORDING & pinned) == [], 'declared and pinned'


def test_the_totalverplant_dashboard_has_no_text_outside_the_catalogue():
    path = ROOT / 'sites/totalverplant-36/template_overrides'
    source = (path / 'site/lan_tournament/dashboard.html').read_text(
        encoding='utf-8'
    )

    # What stays after the Jinja tags and the HTML tags are cut is literal
    # text, which the catalogue never sees.
    literal = HTML_TAG.sub('', JINJA_MARKUP.sub('', source))

    assert LETTER.findall(literal) == []
